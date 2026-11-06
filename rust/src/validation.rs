//! Non-blocking path validation with per-item deadlines and generations.
//!
//! Windows filesystem calls cannot be interrupted. A small worker pool caps
//! ordinary concurrency, one watchdog emits timeout results, and a bounded
//! number of replacement workers preserves service for later local paths if an
//! OS call never returns. Late results are ignored, and generation checks stop
//! an old path from overwriting a re-assigned item.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathStatus {
    Valid,
    Missing,
    /// Windows did not answer in time; the target may still be launchable.
    Timeout,
    Error,
}

impl PathStatus {
    /// Whether the target must be replaced before launching.
    pub fn is_broken(self) -> bool {
        matches!(self, Self::Missing | Self::Error)
    }
}

#[derive(Debug, Clone)]
pub struct ValidationResult {
    pub item_id: String,
    pub path: String,
    #[cfg_attr(not(test), allow(dead_code))]
    pub generation: u64,
    pub status: PathStatus,
}

type ResultCallback = Arc<dyn Fn(ValidationResult) + Send + Sync>;
type ExistsProbe = Arc<dyn Fn(&str) -> Result<bool, String> + Send + Sync>;

struct Task {
    item_id: String,
    path: String,
    generation: u64,
    queued_at: Instant,
    started_at: Option<Instant>,
    done: bool,
    retire_worker: bool,
}

#[derive(Default)]
struct Inner {
    queue: BinaryHeap<(Reverse<u8>, Reverse<u64>, u64)>,
    tasks: HashMap<u64, Task>,
    active: HashMap<String, u64>,
    generations: HashMap<String, u64>,
    next_serial: u64,
    workers_started: bool,
    stranded_workers: usize,
    closed: bool,
}

struct Shared {
    inner: Mutex<Inner>,
    /// Wakes the watchdog when deadlines change.
    deadlines: Condvar,
    /// Wakes workers when work arrives.
    work: Condvar,
    on_result: ResultCallback,
    exists: ExistsProbe,
    timeout: Duration,
    queue_timeout: Duration,
    max_workers: usize,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|poison| poison.into_inner())
    }
}

/// Prefer local work over UNC and device paths when workers are scarce.
pub fn path_queue_priority(path: &str) -> u8 {
    u8::from(path.trim().replace('/', "\\").starts_with("\\\\"))
}

pub struct PathValidationService {
    shared: Arc<Shared>,
}

impl PathValidationService {
    pub fn new(on_result: ResultCallback) -> Self {
        Self::with_probe(
            on_result,
            Arc::new(|path: &str| Ok(std::fs::metadata(path).is_ok())),
            Duration::from_secs(2),
            4,
        )
    }

    pub fn with_probe(
        on_result: ResultCallback,
        exists: ExistsProbe,
        timeout: Duration,
        max_workers: usize,
    ) -> Self {
        Self {
            shared: Arc::new(Shared {
                inner: Mutex::new(Inner::default()),
                deadlines: Condvar::new(),
                work: Condvar::new(),
                on_result,
                exists,
                timeout,
                queue_timeout: (timeout * 2).max(Duration::from_millis(500)),
                max_workers,
            }),
        }
    }

    /// Queue a check, superseding any in-flight check for the same item.
    pub fn validate(&self, item_id: &str, path: &str) {
        let mut inner = self.shared.lock();
        if inner.closed {
            return;
        }
        if let Some(previous) = inner.active.remove(item_id) {
            if let Some(task) = inner.tasks.get_mut(&previous) {
                task.done = true;
            }
            retire_probe(&self.shared, &mut inner, previous);
        }
        let generation = inner.generations.get(item_id).copied().unwrap_or(0) + 1;
        inner.generations.insert(item_id.to_string(), generation);
        inner.next_serial += 1;
        let serial = inner.next_serial;
        inner.tasks.insert(
            serial,
            Task {
                item_id: item_id.to_string(),
                path: path.to_string(),
                generation,
                queued_at: Instant::now(),
                started_at: None,
                done: false,
                retire_worker: false,
            },
        );
        inner.active.insert(item_id.to_string(), serial);
        inner
            .queue
            .push((Reverse(path_queue_priority(path)), Reverse(serial), serial));
        if !inner.workers_started {
            inner.workers_started = true;
            for _ in 0..self.shared.max_workers {
                spawn_worker(&self.shared);
            }
            let shared = Arc::clone(&self.shared);
            let _ = std::thread::Builder::new()
                .name("QuickAccessPathWatchdog".to_string())
                .spawn(move || watchdog_loop(shared));
        }
        self.shared.work.notify_one();
        self.shared.deadlines.notify_all();
    }

    pub fn cancel(&self, item_id: &str) {
        let mut inner = self.shared.lock();
        let next = inner.generations.get(item_id).copied().unwrap_or(0) + 1;
        inner.generations.insert(item_id.to_string(), next);
        if let Some(serial) = inner.active.remove(item_id) {
            if let Some(task) = inner.tasks.get_mut(&serial) {
                task.done = true;
            }
            retire_probe(&self.shared, &mut inner, serial);
        }
        self.shared.deadlines.notify_all();
    }

    pub fn close(&self) {
        let mut inner = self.shared.lock();
        if inner.closed {
            return;
        }
        inner.closed = true;
        inner.active.clear();
        for task in inner.tasks.values_mut() {
            task.done = true;
        }
        self.shared.deadlines.notify_all();
        self.shared.work.notify_all();
    }
}

impl Drop for PathValidationService {
    fn drop(&mut self) {
        self.close();
    }
}

fn spawn_worker(shared: &Arc<Shared>) {
    let shared = Arc::clone(shared);
    let _ = std::thread::Builder::new()
        .name("QuickAccessPathWorker".to_string())
        .spawn(move || worker_loop(shared));
}

/// Replace a stuck worker once, subject to a strict stranded cap.
fn retire_probe(shared: &Arc<Shared>, inner: &mut Inner, serial: u64) {
    if inner.closed || inner.stranded_workers >= shared.max_workers {
        return;
    }
    let Some(task) = inner.tasks.get_mut(&serial) else {
        return;
    };
    if task.started_at.is_none() || task.retire_worker {
        return;
    }
    task.retire_worker = true;
    inner.stranded_workers += 1;
    spawn_worker(shared);
}

fn worker_loop(shared: Arc<Shared>) {
    loop {
        let (serial, path) = {
            let mut inner = shared.lock();
            let serial = loop {
                if inner.closed {
                    return;
                }
                if let Some((_, _, serial)) = inner.queue.pop() {
                    break serial;
                }
                inner = shared.work.wait(inner).unwrap_or_else(|p| p.into_inner());
            };
            let current = inner.active.get(
                &inner.tasks.get(&serial).map(|t| t.item_id.clone()).unwrap_or_default(),
            ) == Some(&serial);
            let Some(task) = inner.tasks.get_mut(&serial) else {
                continue;
            };
            if task.done || !current {
                inner.tasks.remove(&serial);
                continue;
            }
            // Queue residence is excluded from the deadline: a request cannot
            // time out before a worker actually attempted the filesystem call.
            task.started_at = Some(Instant::now());
            let path = task.path.clone();
            shared.deadlines.notify_all();
            (serial, path)
        };

        let probe = (shared.exists)(&path);
        let status = match probe {
            Ok(true) => PathStatus::Valid,
            Ok(false) => PathStatus::Missing,
            Err(_) => PathStatus::Error,
        };
        finish(&shared, serial, status);

        let mut inner = shared.lock();
        let retire = inner.tasks.remove(&serial).is_some_and(|task| task.retire_worker);
        if retire {
            inner.stranded_workers = inner.stranded_workers.saturating_sub(1);
            return;
        }
    }
}

fn watchdog_loop(shared: Arc<Shared>) {
    loop {
        let mut expired: Vec<(u64, bool)> = Vec::new();
        {
            let inner = shared.lock();
            if inner.closed {
                return;
            }
            let deadline_of = |task: &Task| match task.started_at {
                Some(started) => started + shared.timeout,
                None => task.queued_at + shared.queue_timeout,
            };
            let live: Vec<(u64, &Task)> = inner
                .active
                .values()
                .filter_map(|serial| inner.tasks.get(serial).map(|task| (*serial, task)))
                .filter(|(_, task)| !task.done)
                .collect();
            let Some(nearest) = live.iter().map(|(_, task)| deadline_of(task)).min() else {
                drop(live);
                drop(shared.deadlines.wait(inner).unwrap_or_else(|p| p.into_inner()));
                continue;
            };
            let now = Instant::now();
            if nearest > now {
                drop(live);
                drop(
                    shared
                        .deadlines
                        .wait_timeout(inner, nearest - now)
                        .unwrap_or_else(|p| p.into_inner()),
                );
                continue;
            }
            for (serial, task) in &live {
                if deadline_of(task) <= now {
                    expired.push((*serial, task.started_at.is_some()));
                }
            }
        }
        for (serial, started) in expired {
            if started {
                let mut inner = shared.lock();
                retire_probe(&shared, &mut inner, serial);
            }
            finish(&shared, serial, PathStatus::Timeout);
        }
    }
}

fn finish(shared: &Arc<Shared>, serial: u64, status: PathStatus) {
    let result = {
        let mut inner = shared.lock();
        if inner.closed {
            return;
        }
        let Some(task) = inner.tasks.get(&serial) else {
            return;
        };
        if task.done
            || inner.generations.get(&task.item_id) != Some(&task.generation)
            || inner.active.get(&task.item_id) != Some(&serial)
        {
            return;
        }
        let item_id = task.item_id.clone();
        let result = ValidationResult {
            item_id: item_id.clone(),
            path: task.path.clone(),
            generation: task.generation,
            status,
        };
        if let Some(task) = inner.tasks.get_mut(&serial) {
            task.done = true;
        }
        inner.active.remove(&item_id);
        shared.deadlines.notify_all();
        result
    };
    (shared.on_result)(result);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    type Results = mpsc::Receiver<ValidationResult>;

    fn service(
        probe: impl Fn(&str) -> Result<bool, String> + Send + Sync + 'static,
        timeout_ms: u64,
        workers: usize,
    ) -> (PathValidationService, Results) {
        let (sender, receiver) = mpsc::channel();
        let sender = Mutex::new(sender);
        let service = PathValidationService::with_probe(
            Arc::new(move |result| {
                let _ = sender.lock().unwrap().send(result);
            }),
            Arc::new(probe),
            Duration::from_millis(timeout_ms),
            workers,
        );
        (service, receiver)
    }

    fn next(receiver: &Results) -> ValidationResult {
        receiver.recv_timeout(Duration::from_secs(5)).expect("a result")
    }

    #[test]
    fn reports_valid_missing_and_error() {
        let (service, results) = service(
            |path| match path {
                "ok" => Ok(true),
                "gone" => Ok(false),
                _ => Err("boom".to_string()),
            },
            1000,
            2,
        );
        service.validate("1", "ok");
        assert_eq!(next(&results).status, PathStatus::Valid);
        service.validate("2", "gone");
        assert_eq!(next(&results).status, PathStatus::Missing);
        service.validate("3", "other");
        let result = next(&results);
        assert_eq!(result.status, PathStatus::Error);
        assert!(result.status.is_broken());
    }

    #[test]
    fn checks_the_real_filesystem_by_default() {
        let (sender, receiver) = mpsc::channel();
        let sender = Mutex::new(sender);
        let service = PathValidationService::new(Arc::new(move |result| {
            let _ = sender.lock().unwrap().send(result);
        }));
        service.validate("temp", std::env::temp_dir().to_str().unwrap());
        assert_eq!(next(&receiver).status, PathStatus::Valid);
        service.validate("none", r"C:\definitely\missing\path.txt");
        assert_eq!(next(&receiver).status, PathStatus::Missing);
    }

    #[test]
    fn a_hung_probe_times_out_as_a_warning() {
        let (release, gate) = mpsc::channel::<()>();
        let gate = Mutex::new(gate);
        let (service, results) = service(
            move |_| {
                let _ = gate.lock().unwrap().recv();
                Ok(true)
            },
            50,
            1,
        );
        service.validate("1", "hang");
        let result = next(&results);
        assert_eq!(result.status, PathStatus::Timeout);
        assert!(!result.status.is_broken());
        // The late answer is ignored once the deadline has fired.
        release.send(()).unwrap();
        assert!(results.recv_timeout(Duration::from_millis(100)).is_err());
    }

    #[test]
    fn replacement_workers_keep_local_paths_moving() {
        let (_release, gate) = mpsc::channel::<()>();
        let gate = Arc::new(Mutex::new(gate));
        let (service, results) = service(
            move |path| {
                if path == "hang" {
                    let _ = gate.lock().unwrap().recv();
                }
                Ok(true)
            },
            50,
            1,
        );
        service.validate("1", "hang");
        assert_eq!(next(&results).status, PathStatus::Timeout);
        service.validate("2", "fast");
        let result = next(&results);
        assert_eq!(result.item_id, "2");
        assert_eq!(result.status, PathStatus::Valid);
    }

    #[test]
    fn a_new_request_supersedes_the_old_result() {
        let (release, gate) = mpsc::channel::<()>();
        let gate = Mutex::new(gate);
        let (service, results) = service(
            move |path| {
                if path == "slow" {
                    let _ = gate.lock().unwrap().recv();
                }
                Ok(path == "new")
            },
            2000,
            2,
        );
        service.validate("1", "slow");
        std::thread::sleep(Duration::from_millis(30));
        service.validate("1", "new");
        let result = next(&results);
        assert_eq!(result.path, "new");
        assert_eq!(result.generation, 2);
        release.send(()).unwrap();
        assert!(results.recv_timeout(Duration::from_millis(100)).is_err());
    }

    #[test]
    fn cancel_and_close_silence_results() {
        let (release, gate) = mpsc::channel::<()>();
        let gate = Mutex::new(gate);
        let (service, results) = service(
            move |_| {
                let _ = gate.lock().unwrap().recv();
                Ok(true)
            },
            2000,
            1,
        );
        service.validate("1", "x");
        std::thread::sleep(Duration::from_millis(30));
        service.cancel("1");
        release.send(()).unwrap();
        assert!(results.recv_timeout(Duration::from_millis(100)).is_err());
        service.close();
        service.validate("2", "x");
        assert!(results.recv_timeout(Duration::from_millis(50)).is_err());
    }

    #[test]
    fn prioritizes_local_paths() {
        assert_eq!(path_queue_priority(r"C:\x"), 0);
        assert_eq!(path_queue_priority(r"\\srv\x"), 1);
        assert_eq!(path_queue_priority("//srv/x"), 1);
    }
}
