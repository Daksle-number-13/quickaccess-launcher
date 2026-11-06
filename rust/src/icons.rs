//! Shell icon cache that extracts missing icons away from the UI thread.
//!
//! Generic registered-type icons never touch the target, so they run on their
//! own lane. Path-specific extraction (`.exe`, `.ico`, `.lnk`) runs on a single
//! lane guarded by a watchdog: an offline shortcut or misbehaving shell
//! extension can delay only itself, then falls back to the generic icon.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::model::ItemType;
use crate::pyish::{extension, path_identity};

/// A decoded top-down 32-bpp premultiplied BGRA bitmap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IconImage {
    pub width: u32,
    pub height: u32,
    pub bgra: Vec<u8>,
}

pub fn has_visible_alpha(image: &IconImage) -> bool {
    let expected = (image.width as usize) * (image.height as usize) * 4;
    image.width > 0
        && image.height > 0
        && image.bgra.len() == expected
        && image.bgra.chunks_exact(4).any(|pixel| pixel[3] != 0)
}

/// Recover straight BGRA pixels from black and white legacy icon renders.
///
/// Old HICON resources often carry transparency only in their AND mask, which
/// leaves destination alpha at zero. The difference between renders over black
/// and white is the inverse coverage, including antialiased edge pixels.
pub fn recover_legacy_alpha(black: &[u8], white: &[u8]) -> Vec<u8> {
    if black.len() != white.len() || black.len() % 4 != 0 {
        return Vec::new();
    }
    let mut recovered = vec![0_u8; black.len()];
    for (index, (dark, light)) in black.chunks_exact(4).zip(white.chunks_exact(4)).enumerate() {
        let difference = (0..3)
            .map(|channel| i32::from(light[channel]) - i32::from(dark[channel]))
            .max()
            .unwrap_or(0)
            .max(0);
        let alpha = 255 - difference;
        if alpha <= 0 {
            continue;
        }
        let output = &mut recovered[index * 4..index * 4 + 4];
        for channel in 0..3 {
            let component = i32::from(dark[channel]);
            output[channel] = if alpha == 255 {
                component as u8
            } else {
                ((component * 255 + alpha / 2) / alpha).min(255) as u8
            };
        }
        output[3] = alpha as u8;
    }
    recovered
}

pub fn premultiply(pixels: &mut [u8]) {
    for pixel in pixels.chunks_exact_mut(4) {
        let alpha = u32::from(pixel[3]);
        if alpha < 255 {
            for channel in &mut pixel[..3] {
                *channel = ((u32::from(*channel) * alpha + 127) / 255) as u8;
            }
        }
    }
}

/// The cache key: per-type icons share a key; `.exe/.ico/.lnk` are per-path.
pub fn icon_key(path: &str, item_type: ItemType) -> String {
    match item_type {
        ItemType::Folder => "\0folder".to_string(),
        ItemType::Url => "\0url".to_string(),
        ItemType::File => {
            let extension = crate::pyish::strip(extension(path)).to_lowercase();
            if matches!(extension.as_str(), ".exe" | ".ico" | ".lnk") {
                format!("\0path:{}", path_identity(path))
            } else if extension.is_empty() {
                "\0file".to_string()
            } else {
                extension
            }
        }
    }
}

pub fn is_risky_shell_path(path: &str) -> bool {
    let normalized = crate::pyish::strip(path).replace('/', "\\");
    let lowered = normalized.to_lowercase();
    normalized.starts_with("\\\\") || lowered.starts_with("http:\\") || lowered.starts_with("https:\\")
}

pub trait IconApi: Send + Sync {
    fn extract(&self, path: &str, size: u32) -> Option<IconImage>;
    fn extract_generic(&self, path: &str, size: u32, is_directory: bool) -> Option<IconImage>;
}

pub type IconReady = Arc<dyn Fn(String, Arc<IconImage>) + Send + Sync>;

#[derive(Clone)]
struct Candidate {
    serial: u64,
    path: String,
    identity: String,
    attempt: usize,
}

#[derive(Default)]
struct KeyState {
    ready: VecDeque<Candidate>,
    known_paths: HashSet<String>,
    active: Option<u64>,
    pending_retries: usize,
    specific: Option<(u64, Instant)>,
}

struct Work {
    key: String,
    candidate: Candidate,
    generic: bool,
    is_directory: bool,
}

struct Retry {
    at: Instant,
    key: String,
    candidate: Candidate,
}

#[derive(Default)]
struct State {
    cache: HashMap<String, Arc<IconImage>>,
    keys: HashMap<String, KeyState>,
    generic_queue: VecDeque<Work>,
    specific_queue: VecDeque<Work>,
    retries: Vec<Retry>,
    specific_circuit_open: bool,
    next_serial: u64,
    closed: bool,
}

struct Shared {
    state: Mutex<State>,
    generic_ready: Condvar,
    specific_ready: Condvar,
    schedule: Condvar,
    api: Arc<dyn IconApi>,
    on_ready: IconReady,
    size: u32,
    retry_delays: Vec<Duration>,
    specific_timeout: Duration,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poison| poison.into_inner())
    }

    fn enqueue_next(&self, state: &mut State, key: &str) {
        if state.closed {
            return;
        }
        let circuit_open = state.specific_circuit_open;
        let Some(entry) = state.keys.get_mut(key) else {
            return;
        };
        if entry.active.is_some() {
            return;
        }
        let Some(candidate) = entry.ready.pop_front() else {
            return;
        };
        entry.active = Some(candidate.serial);
        let generic = !key.starts_with("\0path:") || is_risky_shell_path(&candidate.path) || circuit_open;
        if !generic {
            entry.specific = Some((candidate.serial, Instant::now() + self.specific_timeout));
        }
        let work = Work {
            key: key.to_string(),
            candidate,
            generic,
            is_directory: key == "\0folder",
        };
        if generic {
            state.generic_queue.push_back(work);
            self.generic_ready.notify_one();
        } else {
            state.specific_queue.push_back(work);
            self.specific_ready.notify_one();
            self.schedule.notify_all();
        }
    }

    fn drop_if_idle(state: &mut State, key: &str) {
        let idle = state.keys.get(key).is_some_and(|entry| {
            entry.active.is_none() && entry.ready.is_empty() && entry.pending_retries == 0
        });
        if idle {
            state.keys.remove(key);
        }
    }
}

pub struct IconService {
    shared: Arc<Shared>,
}

impl IconService {
    pub fn new(api: Arc<dyn IconApi>, on_ready: IconReady) -> Self {
        Self::with_options(api, on_ready, 32, 4, &[0.5, 2.0], Duration::from_millis(500))
    }

    pub fn with_options(
        api: Arc<dyn IconApi>,
        on_ready: IconReady,
        size: u32,
        workers: usize,
        retry_delays: &[f64],
        specific_timeout: Duration,
    ) -> Self {
        let shared = Arc::new(Shared {
            state: Mutex::new(State::default()),
            generic_ready: Condvar::new(),
            specific_ready: Condvar::new(),
            schedule: Condvar::new(),
            api,
            on_ready,
            size,
            retry_delays: retry_delays.iter().map(|d| Duration::from_secs_f64(*d)).collect(),
            specific_timeout,
        });
        for _ in 0..workers {
            let worker = Arc::clone(&shared);
            let _ = std::thread::Builder::new()
                .name("QuickAccessIconGenericWorker".to_string())
                .spawn(move || worker_loop(worker, true));
        }
        let worker = Arc::clone(&shared);
        let _ = std::thread::Builder::new()
            .name("QuickAccessIconSpecificWorker".to_string())
            .spawn(move || worker_loop(worker, false));
        let watchdog = Arc::clone(&shared);
        let _ = std::thread::Builder::new()
            .name("QuickAccessIconWatchdog".to_string())
            .spawn(move || watchdog_loop(watchdog));
        Self { shared }
    }

    #[cfg(test)]
    pub fn get_cached(&self, key: &str) -> Option<Arc<IconImage>> {
        self.shared.lock().cache.get(key).cloned()
    }

    pub fn request(&self, key: &str, path: &str) {
        if path.is_empty() {
            return;
        }
        let identity = path_identity(path);
        let mut guard = self.shared.lock();
        let state = &mut *guard;
        if state.closed || state.cache.contains_key(key) {
            return;
        }
        state.next_serial += 1;
        let serial = state.next_serial;
        let entry = state.keys.entry(key.to_string()).or_default();
        if entry.known_paths.insert(identity.clone()) {
            entry.ready.push_back(Candidate {
                serial,
                path: path.to_string(),
                identity,
                attempt: 0,
            });
        }
        self.shared.enqueue_next(state, key);
    }

    /// Stop retries and workers. An extraction already inside a native API
    /// cannot be aborted; its result is ignored and process exit reclaims it.
    pub fn close(&self) {
        let mut state = self.shared.lock();
        state.closed = true;
        state.keys.clear();
        state.generic_queue.clear();
        state.specific_queue.clear();
        state.retries.clear();
        self.shared.generic_ready.notify_all();
        self.shared.specific_ready.notify_all();
        self.shared.schedule.notify_all();
    }
}

impl Drop for IconService {
    fn drop(&mut self) {
        self.close();
    }
}

fn worker_loop(shared: Arc<Shared>, generic_lane: bool) {
    crate::platform::com_init_thread();
    loop {
        let work = {
            let mut state = shared.lock();
            loop {
                if state.closed {
                    return;
                }
                let queue = if generic_lane {
                    &mut state.generic_queue
                } else {
                    &mut state.specific_queue
                };
                if let Some(work) = queue.pop_front() {
                    let current = state
                        .keys
                        .get(&work.key)
                        .is_some_and(|entry| entry.active == Some(work.candidate.serial));
                    if current {
                        break work;
                    }
                    continue;
                }
                let condvar = if generic_lane { &shared.generic_ready } else { &shared.specific_ready };
                state = condvar.wait(state).unwrap_or_else(|p| p.into_inner());
            }
        };

        let image = if work.generic {
            shared
                .api
                .extract_generic(&work.candidate.path, shared.size, work.is_directory)
        } else {
            shared.api.extract(&work.candidate.path, shared.size)
        }
        .filter(has_visible_alpha);

        let mut delivered = None;
        {
            let mut guard = shared.lock();
            let state = &mut *guard;
            if !work.generic {
                // A returned native call proves the lane is responsive again.
                state.specific_circuit_open = false;
            }
            let current = state
                .keys
                .get(&work.key)
                .is_some_and(|entry| entry.active == Some(work.candidate.serial));
            if state.closed || !current {
                continue;
            }
            let entry = state.keys.get_mut(&work.key).expect("checked above");
            entry.active = None;
            if entry.specific.is_some_and(|(serial, _)| serial == work.candidate.serial) {
                entry.specific = None;
            }
            match image {
                Some(image) => {
                    // Only successes are cached, so a failed shell lookup stays
                    // retryable and cannot poison a shared extension key.
                    let image = Arc::new(image);
                    state.cache.insert(work.key.clone(), Arc::clone(&image));
                    state.retries.retain(|retry| retry.key != work.key);
                    state.keys.remove(&work.key);
                    delivered = Some(image);
                }
                None => {
                    let candidate = work.candidate;
                    if let Some(delay) = shared.retry_delays.get(candidate.attempt) {
                        entry.pending_retries += 1;
                        state.next_serial += 1;
                        let retry = Candidate {
                            serial: state.next_serial,
                            attempt: candidate.attempt + 1,
                            ..candidate
                        };
                        state.retries.push(Retry {
                            at: Instant::now() + *delay,
                            key: work.key.clone(),
                            candidate: retry,
                        });
                        shared.schedule.notify_all();
                    } else {
                        entry.known_paths.remove(&candidate.identity);
                    }
                    // Prefer a different queued source immediately: a bad folder
                    // must not make a valid same-key candidate wait for backoff.
                    shared.enqueue_next(state, &work.key);
                    Shared::drop_if_idle(state, &work.key);
                }
            }
        }
        if let Some(image) = delivered {
            (shared.on_ready)(work.key, image);
        }
    }
}

/// Fires retry timers and moves stalled path-specific work to the safe lane.
fn watchdog_loop(shared: Arc<Shared>) {
    let mut guard = shared.lock();
    loop {
        if guard.closed {
            return;
        }
        let now = Instant::now();
        let state = &mut *guard;

        let mut due = Vec::new();
        state.retries.retain_mut(|retry| {
            if retry.at <= now {
                due.push((retry.key.clone(), retry.candidate.clone()));
                false
            } else {
                true
            }
        });
        for (key, candidate) in due {
            if let Some(entry) = state.keys.get_mut(&key) {
                entry.pending_retries = entry.pending_retries.saturating_sub(1);
                entry.ready.push_back(candidate);
                shared.enqueue_next(state, &key);
            }
        }

        let stalled: Vec<String> = state
            .keys
            .iter()
            .filter(|(_, entry)| entry.specific.is_some_and(|(_, deadline)| deadline <= now))
            .map(|(key, _)| key.clone())
            .collect();
        for key in stalled {
            state.next_serial += 1;
            let fallback_serial = state.next_serial;
            let original = state
                .specific_queue
                .iter()
                .position(|work| work.key == key)
                .and_then(|index| state.specific_queue.remove(index))
                .map(|work| work.candidate);
            let entry = state.keys.get_mut(&key).expect("collected above");
            entry.specific = None;
            entry.active = Some(fallback_serial);
            let path = original
                .as_ref()
                .map(|c| (c.path.clone(), c.identity.clone(), c.attempt))
                .or_else(|| entry.known_paths.iter().next().map(|p| (p.clone(), p.clone(), 0)));
            state.specific_circuit_open = true;
            if let Some((path, identity, attempt)) = path {
                state.generic_queue.push_back(Work {
                    key: key.clone(),
                    candidate: Candidate { serial: fallback_serial, path, identity, attempt },
                    generic: true,
                    is_directory: false,
                });
                shared.generic_ready.notify_one();
            }
        }

        let next_retry = state.retries.iter().map(|retry| retry.at).min();
        let next_specific = state
            .keys
            .values()
            .filter_map(|entry| entry.specific.map(|(_, deadline)| deadline))
            .min();
        let next = [next_retry, next_specific].into_iter().flatten().min();
        guard = match next {
            Some(at) => {
                let wait = at.saturating_duration_since(Instant::now());
                shared
                    .schedule
                    .wait_timeout(guard, wait)
                    .unwrap_or_else(|p| p.into_inner())
                    .0
            }
            None => shared.schedule.wait(guard).unwrap_or_else(|p| p.into_inner()),
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;

    fn pixel() -> IconImage {
        IconImage { width: 1, height: 1, bgra: vec![1, 2, 3, 255] }
    }

    #[test]
    fn builds_stable_keys() {
        assert_eq!(icon_key(r"C:\x", ItemType::Folder), "\0folder");
        assert_eq!(icon_key("https://a", ItemType::Url), "\0url");
        assert_eq!(icon_key(r"C:\a\B.TXT", ItemType::File), ".txt");
        assert_eq!(icon_key(r"C:\a\noext", ItemType::File), "\0file");
        assert_eq!(icon_key(r"C:/A/App.EXE", ItemType::File), "\0path:c:\\a\\app.exe");
    }

    #[test]
    fn recovers_alpha_from_legacy_renders() {
        // Opaque black, fully transparent, and half-covered pixels.
        let black = [0, 0, 0, 0, 0, 0, 0, 0, 50, 50, 50, 0];
        let white = [0, 0, 0, 0, 255, 255, 255, 0, 178, 178, 178, 0];
        let recovered = recover_legacy_alpha(&black, &white);
        assert_eq!(&recovered[0..4], &[0, 0, 0, 255]);
        assert_eq!(recovered[7], 0);
        assert_eq!(recovered[11], 127);
        assert_eq!(recovered[8], 100);
        assert!(recover_legacy_alpha(&[0; 4], &[0; 8]).is_empty());
    }

    #[test]
    fn premultiplies_translucent_pixels_only() {
        let mut pixels = [200, 100, 50, 255, 200, 100, 50, 128, 200, 100, 50, 0];
        premultiply(&mut pixels);
        assert_eq!(&pixels[0..4], &[200, 100, 50, 255]);
        assert_eq!(&pixels[4..8], &[100, 50, 25, 128]);
        assert_eq!(&pixels[8..12], &[0, 0, 0, 0]);
    }

    #[test]
    fn requires_a_visible_pixel() {
        assert!(has_visible_alpha(&pixel()));
        assert!(!has_visible_alpha(&IconImage { width: 1, height: 1, bgra: vec![1, 2, 3, 0] }));
        assert!(!has_visible_alpha(&IconImage { width: 2, height: 1, bgra: vec![1, 2, 3, 255] }));
    }

    #[test]
    fn classifies_risky_paths() {
        assert!(is_risky_shell_path(r"\\srv\a.lnk"));
        assert!(is_risky_shell_path("//srv/a.lnk"));
        assert!(!is_risky_shell_path(r"C:\a.lnk"));
    }

    struct FakeApi {
        extract: Box<dyn Fn(&str) -> Option<IconImage> + Send + Sync>,
        generic: Box<dyn Fn(&str, bool) -> Option<IconImage> + Send + Sync>,
    }

    impl IconApi for FakeApi {
        fn extract(&self, path: &str, _size: u32) -> Option<IconImage> {
            (self.extract)(path)
        }
        fn extract_generic(&self, path: &str, _size: u32, is_directory: bool) -> Option<IconImage> {
            (self.generic)(path, is_directory)
        }
    }

    fn service(api: FakeApi, retries: &[f64], timeout_ms: u64) -> (IconService, mpsc::Receiver<String>) {
        let (sender, receiver) = mpsc::channel();
        let sender = Mutex::new(sender);
        let service = IconService::with_options(
            Arc::new(api),
            Arc::new(move |key, _image| {
                let _ = sender.lock().unwrap().send(key);
            }),
            32,
            2,
            retries,
            Duration::from_millis(timeout_ms),
        );
        (service, receiver)
    }

    #[test]
    fn caches_and_deduplicates_a_shared_key() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(|_| None),
                generic: Box::new(move |_, _| {
                    counter.fetch_add(1, Ordering::SeqCst);
                    std::thread::sleep(Duration::from_millis(20));
                    Some(pixel())
                }),
            },
            &[],
            500,
        );
        service.request(".txt", r"C:\a.txt");
        service.request(".txt", r"C:\a.txt");
        assert_eq!(ready.recv_timeout(Duration::from_secs(5)).unwrap(), ".txt");
        service.request(".txt", r"C:\b.txt");
        assert!(ready.recv_timeout(Duration::from_millis(100)).is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(service.get_cached(".txt").is_some());
    }

    #[test]
    fn routes_specific_and_directory_keys_correctly() {
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(|path| (path == r"C:\app.exe").then(pixel)),
                generic: Box::new(|_, is_directory| is_directory.then(pixel)),
            },
            &[],
            2000,
        );
        service.request("\0path:c:\\app.exe", r"C:\app.exe");
        assert_eq!(ready.recv_timeout(Duration::from_secs(5)).unwrap(), "\0path:c:\\app.exe");
        service.request("\0folder", r"C:\dir");
        assert_eq!(ready.recv_timeout(Duration::from_secs(5)).unwrap(), "\0folder");
    }

    #[test]
    fn retries_failures_until_success() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(|_| None),
                generic: Box::new(move |_, _| {
                    (counter.fetch_add(1, Ordering::SeqCst) == 2).then(pixel)
                }),
            },
            &[0.01, 0.01],
            500,
        );
        service.request(".txt", r"C:\a.txt");
        assert_eq!(ready.recv_timeout(Duration::from_secs(5)).unwrap(), ".txt");
        assert_eq!(calls.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn exhausted_failures_can_be_requested_again() {
        let succeed = Arc::new(AtomicUsize::new(0));
        let flag = Arc::clone(&succeed);
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(|_| None),
                generic: Box::new(move |_, _| (flag.load(Ordering::SeqCst) == 1).then(pixel)),
            },
            &[],
            500,
        );
        service.request(".txt", r"C:\a.txt");
        assert!(ready.recv_timeout(Duration::from_millis(100)).is_err());
        succeed.store(1, Ordering::SeqCst);
        service.request(".txt", r"C:\a.txt");
        assert_eq!(ready.recv_timeout(Duration::from_secs(5)).unwrap(), ".txt");
    }

    #[test]
    fn a_stalled_specific_lane_falls_back_to_generic() {
        let (release, gate) = mpsc::channel::<()>();
        let gate = Mutex::new(gate);
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(move |_| {
                    let _ = gate.lock().unwrap().recv();
                    None
                }),
                generic: Box::new(|_, _| Some(pixel())),
            },
            &[],
            30,
        );
        service.request("\0path:c:\\hang.lnk", r"C:\hang.lnk");
        assert_eq!(
            ready.recv_timeout(Duration::from_secs(5)).unwrap(),
            "\0path:c:\\hang.lnk"
        );
        // A queued second shortcut also bypasses the stuck lane.
        service.request("\0path:c:\\next.lnk", r"C:\next.lnk");
        assert_eq!(
            ready.recv_timeout(Duration::from_secs(5)).unwrap(),
            "\0path:c:\\next.lnk"
        );
        drop(release);
    }

    #[test]
    fn close_stops_further_work() {
        let (service, ready) = service(
            FakeApi {
                extract: Box::new(|_| None),
                generic: Box::new(|_, _| Some(pixel())),
            },
            &[],
            500,
        );
        service.close();
        service.request(".txt", r"C:\a.txt");
        assert!(ready.recv_timeout(Duration::from_millis(50)).is_err());
    }
}
