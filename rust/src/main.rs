//! QuickAccess Launcher: a resident Windows launcher opened with a global hotkey.

#![cfg_attr(not(test), windows_subsystem = "windows")]

mod app;
mod config_transfer;
mod diagnostics;
mod geometry;
mod hotkey;
mod icons;
mod logging;
mod model;
#[cfg(test)]
mod parity_tests;
mod platform;
mod pyish;
mod search;
mod startup_rules;
mod storage;
mod ui;
mod update_rules;
mod url;
mod validation;

use std::path::PathBuf;
use std::time::Instant;

use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
use windows::Win32::System::Threading::{GetCurrentProcess, TerminateProcess};
use windows::Win32::UI::WindowsAndMessaging::{DispatchMessageW, GetMessageW, TranslateMessage, MSG};

use crate::app::{with_app, Event, RunOptions};
use crate::model::{DefaultFolders, ItemType, LauncherConfig};
use crate::platform::singleton::{InstanceRequest, SingleInstanceGuard, MUTEX_NAME};
use crate::storage::ConfigStore;

#[derive(Default)]
struct Arguments {
    startup: bool,
    settings: bool,
    smoke_test: bool,
    capture: Option<PathBuf>,
    benchmark: Option<PathBuf>,
    items: usize,
}

fn parse_arguments() -> Arguments {
    let mut parsed = Arguments { items: 20, ..Default::default() };
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--startup" => parsed.startup = true,
            "--settings" => parsed.settings = true,
            "--smoke-test" => parsed.smoke_test = true,
            "--capture" => parsed.capture = arguments.next().map(PathBuf::from),
            "--benchmark" => parsed.benchmark = arguments.next().map(PathBuf::from),
            "--items" => {
                parsed.items = arguments.next().and_then(|value| value.parse().ok()).unwrap_or(20);
            }
            _ => {}
        }
    }
    parsed
}

/// A temporary directory for isolated smoke, capture and benchmark runs.
struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    fn create() -> std::io::Result<Self> {
        let path = std::env::temp_dir().join(format!(
            "QuickAccessSmoke-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&path)?;
        Ok(Self(path))
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn sample_config(count: usize) -> LauncherConfig {
    let mut config = LauncherConfig::empty();
    let windir = std::env::var("WINDIR").unwrap_or_else(|_| r"C:\Windows".to_string());
    let samples: [(&str, String, ItemType); 8] = [
        ("프로젝트 문서", format!(r"{windir}\System32"), ItemType::Folder),
        ("다운로드", format!(r"{windir}\Temp"), ItemType::Folder),
        ("메모장", format!(r"{windir}\notepad.exe"), ItemType::File),
        ("품질 보고서.xlsx", format!(r"{windir}\report.xlsx"), ItemType::File),
        ("GitHub", "https://github.com".to_string(), ItemType::Url),
        ("아주 긴 이름을 가진 바로가기 항목", format!(r"{windir}\win.ini"), ItemType::File),
        ("네트워크 공유", format!(r"{windir}\Fonts"), ItemType::Folder),
        ("회의록.docx", format!(r"{windir}\missing.docx"), ItemType::File),
    ];
    for index in 0..count {
        let (name, path, kind) = &samples[index % samples.len()];
        let label = if count <= samples.len() { (*name).to_string() } else { format!("{name} {index}") };
        let separator = if *kind == ItemType::Url { '/' } else { '\\' };
        let unique = if index < samples.len() { path.clone() } else { format!("{path}{separator}{index}") };
        config
            .add_item(&unique, &label, Some(*kind), None)
            .expect("sample items are valid");
    }
    config
}

fn run(arguments: &Arguments, started: Instant) -> Result<i32, String> {
    let isolated = arguments.smoke_test || arguments.capture.is_some() || arguments.benchmark.is_some();
    let mut guard = SingleInstanceGuard::new(&if isolated {
        format!("{MUTEX_NAME}-Smoke")
    } else {
        MUTEX_NAME.to_string()
    });
    if !guard.acquire()? {
        if isolated {
            // A release gate must exercise this exact executable.
            log_error!("Another QuickAccess smoke test is already running");
            return Ok(1);
        }
        let request = if arguments.settings {
            InstanceRequest::OpenSettings
        } else {
            InstanceRequest::ShowPanel
        };
        let notified = guard.notify_existing(request);
        log_info!(
            "Existing QuickAccess instance detected; activation {request:?} ({})",
            if notified { "sent" } else { "unavailable" }
        );
        return Ok(0);
    }

    platform::enable_dpi_awareness();
    // SAFETY: first COM use on the UI thread, which stays in this apartment for
    // the process lifetime.
    unsafe {
        CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            .ok()
            .map_err(|error| error.to_string())?;
    }

    let temporary = if isolated {
        Some(TemporaryDirectory::create().map_err(|error| error.to_string())?)
    } else {
        None
    };
    let store = match &temporary {
        Some(directory) => ConfigStore::new(
            directory.0.join("items.json"),
            DefaultFolders::under(&directory.0.to_string_lossy()),
        ),
        None => ConfigStore::new(ConfigStore::default_path(), platform::default_folders()),
    };
    let mut load = store.load().map_err(|error| error.to_string())?;
    if isolated {
        if arguments.capture.is_some() || arguments.benchmark.is_some() {
            let count = if arguments.capture.is_some() { 8 } else { arguments.items };
            load.config = sample_config(count);
        }
        load.config.run_on_startup = false;
        load.config.welcome_shown = true;
        // Isolated from a normally running instance, which usually owns
        // Ctrl+Space already.
        load.config.hotkey = "ctrl+alt+f23".to_string();
        load.config.quick_add_hotkey = "ctrl+alt+f24".to_string();
        store.save(&mut load.config).map_err(|error| error.to_string())?;
    }

    let options = RunOptions { smoke_test: isolated, open_settings: arguments.settings };
    app::install(store, load, &options).map_err(|error| error.to_string())?;
    guard.listen(|request| app::post_event(Event::Instance(request)));
    let ready_ms = started.elapsed().as_secs_f64() * 1000.0;

    if let Some(directory) = &arguments.capture {
        let result = app::dev::capture_all(directory);
        with_app(app::App::shutdown);
        guard.close();
        return result.map(|()| 0);
    }
    if let Some(output) = &arguments.benchmark {
        let result = app::dev::benchmark(output, ready_ms, arguments.items);
        with_app(app::App::shutdown);
        guard.close();
        return result.map(|()| 0);
    }

    let mut message = MSG::default();
    let exit_code = loop {
        // SAFETY: the standard message loop on the thread owning the windows.
        let status = unsafe { GetMessageW(&mut message, None, 0, 0) };
        match status.0 {
            -1 => break 1,
            0 => break message.wParam.0 as i32,
            _ => unsafe {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            },
        }
    };
    guard.close();
    Ok(exit_code)
}

fn main() {
    let started = Instant::now();
    let arguments = parse_arguments();
    logging::configure();
    let _ = arguments.startup;
    let code = match run(&arguments, started) {
        Ok(code) => code,
        Err(error) => {
            log_error!("QuickAccess failed during startup: {error}");
            // A smoke test is an unattended gate; a modal box would hang it.
            if !arguments.smoke_test && arguments.capture.is_none() && arguments.benchmark.is_none() {
                platform::fatal_message(&format!("QuickAccess를 시작하지 못했습니다.\n\n{error}"));
            }
            1
        }
    };
    log_info!("QuickAccess exiting with code {code}");
    // Everything durable is already on disk. ExitProcess would run DLL detach
    // handlers while workers may be stuck in shell or network calls, and has
    // been observed to replace the exit code that release gates rely on.
    // SAFETY: terminating this process with its own pseudo-handle.
    unsafe {
        let _ = TerminateProcess(GetCurrentProcess(), code as u32);
    }
    std::process::exit(code);
}
