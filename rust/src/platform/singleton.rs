//! Single-instance ownership and same-session activation signals.
//!
//! The mutex decides which process owns QuickAccess. Named auto-reset events
//! let a later process ask that owner to show its panel or settings. The names
//! match the Python releases, so either implementation can activate the other.

use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::{
    CreateEventW, CreateMutexW, OpenEventW, SetEvent, WaitForMultipleObjects, EVENT_MODIFY_STATE,
    INFINITE,
};
use windows::Win32::UI::WindowsAndMessaging::AllowSetForegroundWindow;

use crate::pyish::wide;

pub const MUTEX_NAME: &str = "QuickAccessLauncher-2D6B7C9A-0145-4D80-A84C-8297515C16B2";
const ASFW_ANY: u32 = u32::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstanceRequest {
    ShowPanel,
    OpenSettings,
}

pub fn local_mutex_name(name: &str) -> String {
    format!("Local\\{name}")
}

pub fn local_signal_name(name: &str, request: InstanceRequest) -> String {
    let suffix = match request {
        InstanceRequest::ShowPanel => "ShowPanel",
        InstanceRequest::OpenSettings => "OpenSettings",
    };
    format!("{}-Signal-{suffix}", local_mutex_name(name))
}

pub struct SingleInstanceGuard {
    name: String,
    mutex: Option<HANDLE>,
    events: Vec<(InstanceRequest, HANDLE)>,
}

// SAFETY: the kernel handles are process-wide values, valid on any thread.
unsafe impl Send for SingleInstanceGuard {}

impl SingleInstanceGuard {
    pub fn new(name: &str) -> Self {
        Self { name: name.to_string(), mutex: None, events: Vec::new() }
    }

    /// `Ok(true)` when this process became the owner.
    pub fn acquire(&mut self) -> Result<bool, String> {
        let mutex_name = wide(&local_mutex_name(&self.name));
        // SAFETY: the name is NUL-terminated and outlives the call.
        let handle = unsafe { CreateMutexW(None, false, PCWSTR(mutex_name.as_ptr())) }
            .map_err(|error| format!("CreateMutexW failed: {error}"))?;
        // SAFETY: GetLastError reads this thread's value right after the call.
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            // A real handle is returned even for an existing object, and it
            // must not be kept by the losing process.
            // SAFETY: `handle` came from CreateMutexW above.
            unsafe {
                let _ = CloseHandle(handle);
            }
            return Ok(false);
        }
        self.mutex = Some(handle);
        self.prepare_events();
        Ok(true)
    }

    fn prepare_events(&mut self) {
        let mut created = Vec::new();
        for request in [InstanceRequest::ShowPanel, InstanceRequest::OpenSettings] {
            let name = wide(&local_signal_name(&self.name, request));
            // SAFETY: the name is NUL-terminated. Auto-reset events coalesce
            // repeated activation requests without a ResetEvent race.
            match unsafe { CreateEventW(None, false, false, PCWSTR(name.as_ptr())) } {
                Ok(handle) => created.push((request, handle)),
                Err(_) => {
                    // Signaling is a convenience; it never weakens the mutex.
                    for (_, handle) in created {
                        // SAFETY: each handle came from CreateEventW above.
                        unsafe {
                            let _ = CloseHandle(handle);
                        }
                    }
                    return;
                }
            }
        }
        self.events = created;
    }

    /// Ask the existing owner to act; `false` is a clean best-effort fallback.
    pub fn notify_existing(&self, request: InstanceRequest) -> bool {
        let name = wide(&local_signal_name(&self.name, request));
        // SAFETY: the name is NUL-terminated; the opened handle is closed
        // below. Granting foreground rights lets the owner raise its window.
        unsafe {
            let _ = AllowSetForegroundWindow(ASFW_ANY);
            let Ok(handle) = OpenEventW(EVENT_MODIFY_STATE, false, PCWSTR(name.as_ptr())) else {
                return false;
            };
            let delivered = SetEvent(handle).is_ok();
            let _ = CloseHandle(handle);
            delivered
        }
    }

    /// Block a background thread on the events, forwarding each request.
    pub fn listen(&self, on_request: impl Fn(InstanceRequest) + Send + 'static) {
        if self.events.is_empty() {
            return;
        }
        let requests: Vec<InstanceRequest> = self.events.iter().map(|(request, _)| *request).collect();
        let raw: Vec<isize> = self.events.iter().map(|(_, handle)| handle.0 as isize).collect();
        let _ = std::thread::Builder::new()
            .name("QuickAccessInstanceSignals".to_string())
            .spawn(move || {
                let handles: Vec<HANDLE> = raw.iter().map(|value| HANDLE(*value as *mut _)).collect();
                loop {
                    // SAFETY: the handles stay open for the process lifetime,
                    // since `close` runs only as the process exits.
                    let result = unsafe { WaitForMultipleObjects(&handles, false, INFINITE) };
                    let index = result.0.wrapping_sub(WAIT_OBJECT_0.0) as usize;
                    match requests.get(index) {
                        Some(request) => on_request(*request),
                        None => return,
                    }
                }
            });
    }

    pub fn close(&mut self) {
        // Close activation events before releasing the authoritative mutex so
        // a new owner never targets endpoints from the old one.
        for (_, handle) in self.events.drain(..) {
            // SAFETY: each handle is owned by this guard and closed once.
            unsafe {
                let _ = CloseHandle(handle);
            }
        }
        if let Some(handle) = self.mutex.take() {
            // SAFETY: as above.
            unsafe {
                let _ = CloseHandle(handle);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    fn unique_name() -> String {
        format!("QuickAccessTest-{}", uuid::Uuid::new_v4().simple())
    }

    #[test]
    fn names_match_the_python_release() {
        assert_eq!(local_mutex_name("x"), "Local\\x");
        assert_eq!(local_signal_name("x", InstanceRequest::ShowPanel), "Local\\x-Signal-ShowPanel");
        assert_eq!(
            local_signal_name("x", InstanceRequest::OpenSettings),
            "Local\\x-Signal-OpenSettings"
        );
    }

    #[test]
    fn second_instance_loses_and_signals_the_owner() {
        let name = unique_name();
        let mut owner = SingleInstanceGuard::new(&name);
        assert_eq!(owner.acquire(), Ok(true));
        let (sender, receiver) = mpsc::channel();
        owner.listen(move |request| {
            let _ = sender.send(request);
        });

        let mut second = SingleInstanceGuard::new(&name);
        assert_eq!(second.acquire(), Ok(false));
        assert!(second.notify_existing(InstanceRequest::OpenSettings));
        assert_eq!(
            receiver.recv_timeout(Duration::from_secs(5)).unwrap(),
            InstanceRequest::OpenSettings
        );
        assert!(second.notify_existing(InstanceRequest::ShowPanel));
        assert_eq!(
            receiver.recv_timeout(Duration::from_secs(5)).unwrap(),
            InstanceRequest::ShowPanel
        );
    }

    #[test]
    fn released_mutex_can_be_reacquired() {
        let name = unique_name();
        let mut first = SingleInstanceGuard::new(&name);
        assert_eq!(first.acquire(), Ok(true));
        first.close();
        let mut second = SingleInstanceGuard::new(&name);
        assert_eq!(second.acquire(), Ok(true));
        second.close();
    }

    #[test]
    fn notifying_without_an_owner_is_a_clean_false() {
        let guard = SingleInstanceGuard::new(&unique_name());
        assert!(!guard.notify_existing(InstanceRequest::ShowPanel));
    }
}
