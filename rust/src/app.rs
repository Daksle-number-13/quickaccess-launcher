//! Application controller: owns the state and coordinates windows and services.
//!
//! Everything lives on one UI thread, reached through [`with_app`]. Win32 calls
//! such as `SetWindowPos` re-enter window procedures synchronously, so nested
//! access returns `None` rather than panicking; the outer code handles those
//! effects explicitly. Modal work (dialogs, menus, file pickers) runs a nested
//! message loop, so it is expressed as an [`Action`] executed outside the borrow.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicIsize, AtomicUsize, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use windows::core::w;
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    RegisterHotKey, UnregisterHotKey, HOT_KEY_MODIFIERS, MOD_NOREPEAT,
};
use windows::Win32::UI::WindowsAndMessaging::{
    AllowSetForegroundWindow, CreateWindowExW, DefWindowProcW, KillTimer, PostMessageW, PostQuitMessage,
    RegisterClassW, SetTimer, WINDOW_EX_STYLE, WM_APP, WM_HOTKEY, WM_LBUTTONUP, WM_RBUTTONUP,
    WM_SETTINGCHANGE, WM_TIMER, WNDCLASSW, WS_POPUP,
};

use crate::config_transfer::{preview_config_import, write_portable_config, ImportMode, ImportPreview};
use crate::diagnostics::{self, APP_VERSION};
use crate::geometry::Point;
use crate::hotkey::{Binding, HotkeyApi, HotkeyManager, ParsedHotkey};
use crate::icons::{icon_key, IconImage, IconService};
use crate::model::{normalize_web_url, AppearanceMode, ItemType, LauncherConfig};
use crate::platform::explorer::{resolve_target, ExplorerTarget};
use crate::platform::native_icons::NativeIconApi;
use crate::platform::registry::{os_description, StartupManager};
use crate::platform::singleton::InstanceRequest;
use crate::platform::{self, dialogs, tray, MessageKind};
use crate::pyish::{display_name, path_identity};
use crate::startup_rules::{RegistrationState, RegistrationStatus};
use crate::storage::{ConfigStore, LoadResult};
use crate::ui::dialog::{ask_display_name, ask_text};
use crate::ui::kit::guard_panic;
use crate::ui::popup::{run_copy_path_menu, Popup, ViewData};
use crate::ui::settings::{Settings, Tone};
use crate::ui::theme::{palette_for, Palette};
use crate::ui::toast::{Toast, ToastAction, ToastKind};
use crate::update_rules::{check_for_update, UpdateCheckResult, DEFAULT_REPO};
use crate::validation::{PathStatus, PathValidationService, ValidationResult};
use crate::{log_error, log_info, log_warn};

const WM_APP_WAKE: u32 = WM_APP + 1;
const WM_APP_TRAY: u32 = WM_APP + 2;
const QUICK_ADD_TIMEOUT_MS: u32 = 8000;
const MAX_CONCURRENT_QUICK_ADDS: usize = 2;
const MAX_CONCURRENT_LAUNCHES: usize = 4;
const MAX_IMPORT_BYTES: u64 = 2 * 1024 * 1024;
const INTEGRATION_RETRY_DELAYS_MS: [u32; 3] = [2000, 5000, 15000];
const ASFW_ANY: u32 = u32::MAX;

/// Background results delivered to the UI thread.
pub enum Event {
    Validation(ValidationResult),
    Icon(String, Arc<IconImage>),
    Launch { item_id: String, item_name: String, result: Result<(), String> },
    QuickAdd { generation: u64, result: Result<Vec<ExplorerTarget>, String> },
    Update { generation: u64, result: UpdateCheckResult },
    Instance(InstanceRequest),
    RequestIcons,
}

/// Work that must run outside the application borrow.
pub enum Action {
    OpenSettings,
    ActivateItem(String),
    RelocateItem(String),
    CopyPathMenu { name: String, path: String },
    AddFolder,
    AddFile,
    AddLink,
    EditItem(String),
    DeleteItem(String),
    MoveItem(String, i64),
    SetColumns(i64),
    SetAppearance(AppearanceMode),
    SetStartup(bool),
    SetUpdates(bool),
    ApplyHotkeys(String, String),
    ImportConfig,
    ExportConfig,
    CopyDiagnostics,
    RequestShutdown,
    RunToastAction(ToastAction),
    QuickAddOne(ExplorerTarget),
    TrayMenu,
}

enum Deferred {
    Toast { message: String, kind: ToastKind, duration_ms: u32 },
    DismissToast(u64),
    ExpireQuickAdd(u64),
    CheckUpdate,
    OpenSettings,
    FinishSmoke,
    RetryIntegrations,
    CheckPopupFocus(u64),
}

struct NativeHotkeys(HWND);

impl HotkeyApi for NativeHotkeys {
    fn register(&mut self, id: i32, hotkey: &ParsedHotkey) -> Result<(), String> {
        let modifiers = HOT_KEY_MODIFIERS(hotkey.modifiers) | MOD_NOREPEAT;
        // SAFETY: registering against a window owned by this thread.
        unsafe { RegisterHotKey(Some(self.0), id, modifiers, hotkey.virtual_key) }
            .map_err(|error| error.message().to_string())
    }

    fn unregister(&mut self, id: i32) -> Result<(), String> {
        // SAFETY: as above.
        unsafe { UnregisterHotKey(Some(self.0), id) }.map_err(|error| error.message().to_string())
    }
}

/// Bounded permits for fire-and-forget workers.
#[derive(Clone)]
struct Slots(Arc<AtomicUsize>, usize);

impl Slots {
    fn new(limit: usize) -> Self {
        Self(Arc::new(AtomicUsize::new(0)), limit)
    }

    fn try_acquire(&self) -> bool {
        let mut used = self.0.load(Ordering::SeqCst);
        while used < self.1 {
            match self.0.compare_exchange(used, used + 1, Ordering::SeqCst, Ordering::SeqCst) {
                Ok(_) => return true,
                Err(actual) => used = actual,
            }
        }
        false
    }

    fn release(&self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

pub struct RunOptions {
    pub smoke_test: bool,
    pub open_settings: bool,
}

pub struct App {
    pub config: LauncherConfig,
    pub statuses: HashMap<String, PathStatus>,
    pub icons: HashMap<String, Arc<IconImage>>,
    pub popup: Popup,
    pub toast: Toast,
    settings: Option<Settings>,
    store: ConfigStore,
    generation: u64,
    palette: &'static Palette,
    main_hwnd: HWND,
    hotkeys: HotkeyManager,
    tray_ready: bool,
    taskbar_created: u32,
    validator: PathValidationService,
    icon_service: IconService,
    startup: StartupManager,
    startup_status: Option<RegistrationStatus>,
    launch_inflight: HashSet<String>,
    launch_slots: Slots,
    quick_add_inflight: bool,
    quick_add_generation: u64,
    quick_add_slots: Slots,
    update_generation: u64,
    update_inflight: HashSet<u64>,
    timers: HashMap<usize, Deferred>,
    next_timer: usize,
    toast_generation: u64,
    stopping: bool,
    smoke_test: bool,
    /// Screenshots show this instead of the machine's real Run value.
    preview_startup: Option<RegistrationStatus>,
    integration_attempts: usize,
    outage_notified: bool,
    events: Receiver<Event>,
}

thread_local! {
    static APP: RefCell<Option<App>> = const { RefCell::new(None) };
}

static SENDER: OnceLock<Mutex<Sender<Event>>> = OnceLock::new();
static MAIN_HWND: AtomicIsize = AtomicIsize::new(0);

/// Queue a result for the UI thread from any thread.
pub fn post_event(event: Event) {
    let Some(sender) = SENDER.get() else {
        return;
    };
    if sender.lock().unwrap_or_else(|p| p.into_inner()).send(event).is_err() {
        return;
    }
    let hwnd = MAIN_HWND.load(Ordering::SeqCst);
    if hwnd != 0 {
        // SAFETY: posting to a window handle; a stale handle just fails.
        unsafe {
            let _ = PostMessageW(Some(HWND(hwnd as *mut _)), WM_APP_WAKE, WPARAM(0), LPARAM(0));
        }
    }
}

/// Run `body` against the app, or return `None` when re-entered during a borrow.
pub fn with_app<R>(body: impl FnOnce(&mut App) -> R) -> Option<R> {
    APP.with(|cell| {
        let mut guard = cell.try_borrow_mut().ok()?;
        guard.as_mut().map(body)
    })
}

/// Handle a message, then run any resulting modal work outside the borrow.
pub fn dispatch(body: impl FnOnce(&mut App) -> Vec<Action>) {
    if let Some(actions) = with_app(body) {
        run_actions(actions);
    }
}

pub fn run_actions(actions: Vec<Action>) {
    for action in actions {
        run_action(action);
    }
}

fn startup_invocation() -> (String, Vec<&'static str>) {
    let executable = std::env::current_exe()
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_default();
    (executable, vec!["--startup"])
}

unsafe extern "system" fn main_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    guard_panic(LRESULT(0), || {
        match message {
            WM_APP_WAKE => {
                dispatch(App::drain_events);
                return LRESULT(0);
            }
            WM_HOTKEY => {
                dispatch(|app| app.on_hotkey(wparam.0 as i32));
                return LRESULT(0);
            }
            WM_APP_TRAY => {
                match (lparam.0 & 0xFFFF) as u32 {
                    WM_LBUTTONUP => {
                        with_app(App::open_panel);
                    }
                    WM_RBUTTONUP => run_action(Action::TrayMenu),
                    _ => {}
                }
                return LRESULT(0);
            }
            WM_TIMER => {
                dispatch(|app| app.on_timer(hwnd, wparam.0));
                return LRESULT(0);
            }
            WM_SETTINGCHANGE => {
                with_app(App::on_setting_change);
            }
            _ => {
                let taskbar = with_app(|app| app.taskbar_created).unwrap_or(0);
                if taskbar != 0 && message == taskbar {
                    with_app(App::on_taskbar_created);
                    return LRESULT(0);
                }
            }
        }
        // SAFETY: forwarding unmodified parameters for this window.
        unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
    })
}

fn create_main_window() -> windows::core::Result<HWND> {
    // SAFETY: standard class registration and window creation on the UI thread.
    unsafe {
        let instance: HINSTANCE = GetModuleHandleW(None)?.into();
        let class = WNDCLASSW {
            lpfnWndProc: Some(main_proc),
            hInstance: instance,
            lpszClassName: w!("QuickAccessMain"),
            ..Default::default()
        };
        RegisterClassW(&class);
        // A hidden top-level window, not message-only: it must receive the
        // TaskbarCreated and WM_SETTINGCHANGE broadcasts.
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("QuickAccessMain"),
            w!("QuickAccess Launcher"),
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            Some(instance),
            None,
        )
    }
}

/// Build the app and its windows, then install it as the thread's instance.
pub fn install(store: ConfigStore, load: LoadResult, options: &RunOptions) -> windows::core::Result<()> {
    let (sender, events) = channel();
    let _ = SENDER.set(Mutex::new(sender));
    let main_hwnd = create_main_window()?;
    MAIN_HWND.store(main_hwnd.0 as isize, Ordering::SeqCst);

    let palette = palette_for(load.config.appearance_mode);
    let app = App {
        config: load.config.clone(),
        statuses: HashMap::new(),
        icons: HashMap::new(),
        popup: Popup::create()?,
        toast: Toast::create()?,
        settings: None,
        store,
        generation: 1,
        palette,
        main_hwnd,
        hotkeys: HotkeyManager::default(),
        tray_ready: false,
        taskbar_created: tray::taskbar_created_message(),
        validator: PathValidationService::new(Arc::new(|result| post_event(Event::Validation(result)))),
        icon_service: IconService::new(
            Arc::new(NativeIconApi),
            Arc::new(|key, image| post_event(Event::Icon(key, image))),
        ),
        startup: StartupManager,
        startup_status: None,
        launch_inflight: HashSet::new(),
        launch_slots: Slots::new(MAX_CONCURRENT_LAUNCHES),
        quick_add_inflight: false,
        quick_add_generation: 0,
        quick_add_slots: Slots::new(MAX_CONCURRENT_QUICK_ADDS),
        update_generation: 0,
        update_inflight: HashSet::new(),
        timers: HashMap::new(),
        next_timer: 1,
        toast_generation: 0,
        stopping: false,
        smoke_test: options.smoke_test,
        preview_startup: None,
        integration_attempts: 0,
        outage_notified: false,
        events,
    };
    APP.with(|cell| *cell.borrow_mut() = Some(app));
    with_app(|app| app.start(&load, options));
    Ok(())
}

impl App {
    pub fn palette(&self) -> &'static Palette {
        self.palette
    }

    pub fn settings_mut(&mut self) -> Option<&mut Settings> {
        self.settings.as_mut()
    }

    fn defer(&mut self, delay_ms: u32, action: Deferred) {
        let id = self.next_timer;
        self.next_timer += 1;
        self.timers.insert(id, action);
        // SAFETY: arming a timer on a window owned by this thread.
        unsafe {
            SetTimer(Some(self.main_hwnd), id, delay_ms.max(1), None);
        }
    }

    fn start(&mut self, load: &LoadResult, options: &RunOptions) {
        // Reconcile the preference with HKCU Run on every normal launch. This
        // also repairs the command after the EXE is moved or renamed.
        if !self.smoke_test {
            self.synchronize_startup_registration();
        }

        // Finish the popup before the hotkeys go live, so a hotkey can never
        // race its first render.
        let monitor = platform::monitor_at_cursor();
        self.with_popup_view(|popup, data, generation| popup.prepare(data, generation, &monitor));

        let hotkey_ready = self.configure_hotkeys(&self.config.hotkey.clone(), &self.config.quick_add_hotkey.clone(), true);
        if self.smoke_test && !hotkey_ready {
            log_error!("smoke test could not register global hotkeys");
            self.exit_with(1);
            return;
        }
        match tray::add(self.main_hwnd, WM_APP_TRAY, "QuickAccess Launcher") {
            Ok(()) => self.tray_ready = true,
            Err(error) => {
                log_warn!("Unable to start system tray: {error}");
                self.show_toast(&format!("시스템 트레이를 시작하지 못했습니다: {error}"), ToastKind::Error, 3200, None);
                if self.smoke_test {
                    log_error!("smoke test could not start the system tray");
                    self.exit_with(1);
                    return;
                }
            }
        }
        if !self.smoke_test && !(hotkey_ready && self.tray_ready) {
            self.defer(INTEGRATION_RETRY_DELAYS_MS[0], Deferred::RetryIntegrations);
        }

        self.validate_all_paths();
        self.request_all_icons();
        if !self.smoke_test && self.config.check_updates {
            // Delayed so a slow or firewalled network call never competes with
            // hotkey and tray registration.
            self.defer(5000, Deferred::CheckUpdate);
        }

        let backup_name = load
            .backup_path
            .as_ref()
            .and_then(|path| path.file_name())
            .map(|name| name.to_string_lossy().into_owned());
        if load.recovered {
            let mut message = if load.restored_from_backup {
                "설정 파일이 손상되어 이전 백업으로 복구했습니다.".to_string()
            } else {
                "설정 파일이 손상되어 기본값으로 복구했습니다.".to_string()
            };
            if let Some(name) = &backup_name {
                message += &format!("\n백업: {name}");
            }
            self.defer(350, Deferred::Toast { message, kind: ToastKind::Warning, duration_ms: 5500 });
        } else if load.repaired {
            let mut message = "읽을 수 없는 일부 항목을 제외하고 설정을 복구했습니다.".to_string();
            if let Some(name) = &backup_name {
                message += &format!("\n원본 백업: {name}");
            }
            self.defer(350, Deferred::Toast { message, kind: ToastKind::Warning, duration_ms: 5500 });
        }
        if load.created && !self.config.welcome_shown {
            self.commit(|config| { config.welcome_shown = true; Ok(()) }, "최초 실행 상태를 저장하지 못했습니다");
            self.defer(
                650,
                Deferred::Toast {
                    message: "QuickAccess가 실행 중입니다.\nCtrl+Space로 런처를 열 수 있습니다.".to_string(),
                    kind: ToastKind::Info,
                    duration_ms: 5000,
                },
            );
        }
        // If both entry mechanisms failed, expose settings instead of leaving
        // an unreachable resident process.
        if !hotkey_ready && !self.tray_ready {
            self.outage_notified = true;
            self.defer(100, Deferred::OpenSettings);
        } else if options.open_settings {
            self.defer(1, Deferred::OpenSettings);
        }
        if self.smoke_test {
            self.defer(2500, Deferred::FinishSmoke);
        }
        log_info!("QuickAccess started");
    }

    fn with_popup_view(&mut self, body: impl FnOnce(&mut Popup, &ViewData<'_>, u64)) {
        let Self { popup, config, statuses, icons, palette, generation, .. } = self;
        let data = ViewData { config, statuses, icons, palette };
        body(popup, &data, *generation);
    }

    pub fn paint_popup(&mut self) {
        self.with_popup_view(|popup, data, _| popup.paint(data));
    }

    pub fn paint_settings(&mut self) {
        let palette = self.palette;
        let Self { settings, config, .. } = self;
        if let Some(settings) = settings {
            settings.paint(config, palette);
        }
    }

    pub fn paint_toast(&mut self) {
        let palette = self.palette;
        self.toast.paint(palette);
    }

    pub fn show_toast(&mut self, message: &str, kind: ToastKind, duration_ms: u32, action: Option<(String, ToastAction)>) {
        let monitor = platform::monitor_at_cursor();
        self.toast.show(message, kind, action, self.palette, &monitor);
        self.toast_generation += 1;
        self.defer(duration_ms.max(500), Deferred::DismissToast(self.toast_generation));
    }

    fn toast_simple(&mut self, message: &str, kind: ToastKind) {
        self.show_toast(message, kind, 3200, None);
    }

    fn on_timer(&mut self, hwnd: HWND, id: usize) -> Vec<Action> {
        // SAFETY: stopping a timer on a window owned by this thread.
        unsafe {
            let _ = KillTimer(Some(hwnd), id);
        }
        let Some(deferred) = self.timers.remove(&id) else {
            return Vec::new();
        };
        match deferred {
            Deferred::Toast { message, kind, duration_ms } => self.show_toast(&message, kind, duration_ms, None),
            Deferred::DismissToast(generation) => {
                if generation == self.toast_generation {
                    self.toast.close();
                }
            }
            Deferred::ExpireQuickAdd(generation) => self.expire_quick_add(generation),
            Deferred::CheckUpdate => self.check_for_update(),
            Deferred::OpenSettings => return vec![Action::OpenSettings],
            Deferred::FinishSmoke => self.exit_with(0),
            Deferred::RetryIntegrations => self.retry_integrations(),
            Deferred::CheckPopupFocus(generation) => {
                let focused = platform::foreground_window() == self.popup.hwnd.0 as isize;
                if generation == self.popup.show_generation && !focused {
                    self.popup.hide(false);
                }
            }
        }
        Vec::new()
    }

    /// Hide on focus loss, re-checking later during the show transition.
    pub fn on_popup_deactivated(&mut self) {
        if self.popup.suppress_deactivate || !self.popup.visible {
            return;
        }
        if self.popup.is_arming() {
            let generation = self.popup.show_generation;
            self.defer(crate::ui::popup::FOCUS_ARMING_MS, Deferred::CheckPopupFocus(generation));
        } else {
            self.popup.hide(false);
        }
    }

    fn on_setting_change(&mut self) {
        let palette = palette_for(self.config.appearance_mode);
        if palette.dark != self.palette.dark {
            self.set_palette(palette);
        }
    }

    fn set_palette(&mut self, palette: &'static Palette) {
        self.palette = palette;
        if let Some(settings) = &self.settings {
            settings.apply_palette(palette);
            settings.invalidate();
        }
        self.popup.invalidate();
    }

    fn on_taskbar_created(&mut self) {
        // Explorer restarted, which removed every notification icon.
        match tray::add(self.main_hwnd, WM_APP_TRAY, "QuickAccess Launcher") {
            Ok(()) => self.tray_ready = true,
            Err(error) => {
                self.tray_ready = false;
                log_warn!("Unable to restore the tray icon: {error}");
                self.defer(INTEGRATION_RETRY_DELAYS_MS[0], Deferred::RetryIntegrations);
            }
        }
    }

    fn retry_integrations(&mut self) {
        if self.stopping {
            return;
        }
        if !self.hotkeys.is_complete() {
            let (panel, quick) = (self.config.hotkey.clone(), self.config.quick_add_hotkey.clone());
            if self.configure_hotkeys(&panel, &quick, false) {
                self.show_toast("전역 단축키를 자동으로 복구했습니다.", ToastKind::Success, 3200, None);
            }
        }
        if !self.tray_ready && tray::add(self.main_hwnd, WM_APP_TRAY, "QuickAccess Launcher").is_ok() {
            self.tray_ready = true;
            self.show_toast("시스템 트레이를 자동으로 복구했습니다.", ToastKind::Success, 3200, None);
        }
        if self.hotkeys.is_complete() && self.tray_ready {
            self.integration_attempts = 0;
            self.outage_notified = false;
            return;
        }
        if let Some(delay) = INTEGRATION_RETRY_DELAYS_MS.get(self.integration_attempts + 1) {
            self.integration_attempts += 1;
            self.defer(*delay, Deferred::RetryIntegrations);
        } else {
            let label = if self.hotkeys.is_complete() { "시스템 트레이" } else { "전역 단축키" };
            self.show_toast(
                &format!("{label} 자동 복구에 실패했습니다. 프로그램을 종료한 뒤 다시 실행해 주세요."),
                ToastKind::Warning,
                6500,
                None,
            );
        }
    }

    pub fn drain_events(&mut self) -> Vec<Action> {
        let mut actions = Vec::new();
        while let Ok(event) = self.events.try_recv() {
            match event {
                Event::Validation(result) => self.apply_validation(result),
                Event::Icon(key, image) => {
                    self.icons.entry(key).or_insert(image);
                    self.refresh_visible(false);
                }
                Event::Launch { item_id, item_name, result } => {
                    self.launch_inflight.remove(&item_id);
                    if let Err(error) = result {
                        self.toast_simple(&format!("'{item_name}'을(를) 열지 못했습니다: {error}"), ToastKind::Error);
                    }
                }
                Event::QuickAdd { generation, result } => {
                    if let Some(action) = self.finish_quick_add(generation, result) {
                        actions.push(action);
                    }
                }
                Event::Update { generation, result } => self.apply_update_check(generation, result),
                Event::Instance(InstanceRequest::ShowPanel) => self.open_panel(),
                Event::Instance(InstanceRequest::OpenSettings) => actions.push(Action::OpenSettings),
                Event::RequestIcons => self.request_all_icons(),
            }
        }
        actions
    }

    fn on_hotkey(&mut self, id: i32) -> Vec<Action> {
        match self.hotkeys.binding_for(id) {
            Some(Binding::Panel) => self.open_panel(),
            Some(Binding::QuickAdd) => self.begin_quick_add(platform::foreground_window()),
            None => {}
        }
        Vec::new()
    }

    fn configure_hotkeys(&mut self, panel: &str, quick_add: &str, show_error: bool) -> bool {
        let mut api = NativeHotkeys(self.main_hwnd);
        match self.hotkeys.configure(&mut api, panel, quick_add) {
            Ok(()) => true,
            Err(error) => {
                log_error!("Unable to configure global hotkeys: {error}");
                if show_error {
                    self.show_toast(
                        &format!("전역 핫키를 등록하지 못했습니다: {error}\n트레이의 설정에서 다른 키를 선택해 주세요."),
                        ToastKind::Warning,
                        6500,
                        None,
                    );
                }
                false
            }
        }
    }

    /// Open at the pointer's position at this moment, not when the key fired.
    pub fn open_panel(&mut self) {
        let anchor = platform::cursor_position().unwrap_or(Point { x: 20, y: 20 });
        let monitor = platform::monitor_at(anchor).unwrap_or_else(platform::MonitorContext::fallback);
        self.with_popup_view(|popup, data, generation| popup.show(data, generation, anchor, &monitor));
        // Extraction stays off the hotkey path, but a transient shell failure
        // can still recover on a later panel open.
        post_event(Event::RequestIcons);
    }

    fn refresh_visible(&mut self, layout_required: bool) {
        if layout_required {
            self.generation += 1;
            self.with_popup_view(|popup, data, generation| popup.refresh_layout(data, generation));
        } else if self.popup.visible {
            self.popup.invalidate();
        }
        if let Some(settings) = &self.settings {
            if settings.is_visible() {
                settings.invalidate();
            }
        }
    }

    /// Apply a mutation to a copy, persist it, then adopt it.
    fn commit(
        &mut self,
        mutator: impl FnOnce(&mut LauncherConfig) -> Result<(), crate::model::ConfigError>,
        error_message: &str,
    ) -> bool {
        let mut candidate = self.config.clone();
        let result = mutator(&mut candidate)
            .map_err(|error| error.to_string())
            .and_then(|()| self.store.save(&mut candidate).map_err(|error| error.to_string()));
        match result {
            Ok(()) => {
                self.config = candidate;
                self.refresh_visible(true);
                true
            }
            Err(error) => {
                log_error!("{error_message}: {error}");
                self.toast_simple(&format!("{error_message}: {error}"), ToastKind::Error);
                false
            }
        }
    }

    fn validate_item(&mut self, item_id: &str) {
        self.statuses.remove(item_id);
        let Some(item) = self.config.get_item(item_id) else {
            return;
        };
        if item.item_type != ItemType::Url {
            let (path, kind) = (item.path.clone(), item.item_type);
            self.validator.validate(item_id, &path);
            self.icon_service.request(&icon_key(&path, kind), &path);
        }
    }

    fn validate_all_paths(&mut self) {
        for item in &self.config.items {
            if item.item_type != ItemType::Url {
                self.validator.validate(&item.id, &item.path);
            }
        }
    }

    fn request_all_icons(&mut self) {
        for item in &self.config.items {
            if item.item_type != ItemType::Url {
                self.icon_service.request(&icon_key(&item.path, item.item_type), &item.path);
            }
        }
    }

    fn apply_validation(&mut self, result: ValidationResult) {
        let Some(current) = self.config.get_item(&result.item_id) else {
            return;
        };
        if current.path != result.path {
            return;
        }
        self.statuses.insert(result.item_id, result.status);
        self.refresh_visible(false);
    }

    pub fn add_item(&mut self, path: &str, name: &str, item_type: Option<ItemType>) -> bool {
        let new_id = RefCell::new(String::new());
        let saved = self.commit(
            |config| {
                *new_id.borrow_mut() = config.add_item(path, name, item_type, None)?;
                Ok(())
            },
            "항목을 저장하지 못했습니다",
        );
        if saved {
            self.validate_item(&new_id.into_inner());
        }
        saved
    }

    pub fn delete_item(&mut self, item_id: &str) -> bool {
        let Some(item) = self.config.get_item(item_id).cloned() else {
            return false;
        };
        if !self.commit(|config| config.delete_item(item_id).map(|_| ()), "항목을 삭제하지 못했습니다") {
            return false;
        }
        self.validator.cancel(item_id);
        self.statuses.remove(item_id);
        self.show_toast(
            &format!("'{}' 항목을 삭제했습니다.", item.name),
            ToastKind::Warning,
            6000,
            Some((
                "실행취소".to_string(),
                ToastAction::Restore {
                    name: item.name,
                    path: item.path,
                    item_type: item.item_type,
                    order: item.order,
                },
            )),
        );
        true
    }

    fn restore_deleted_item(&mut self, name: &str, path: &str, item_type: ItemType, order: i64) {
        let new_id = RefCell::new(String::new());
        let saved = self.commit(
            |config| {
                *new_id.borrow_mut() = config.add_item(path, name, Some(item_type), Some(order))?;
                Ok(())
            },
            "항목을 복구하지 못했습니다",
        );
        if !saved {
            return;
        }
        self.validate_item(&new_id.into_inner());
        self.refresh_settings();
        self.toast_simple(&format!("'{name}' 항목을 복구했습니다."), ToastKind::Success);
    }

    pub fn refresh_settings(&mut self) {
        let visible = self.settings.as_ref().is_some_and(Settings::is_visible);
        if !visible {
            return;
        }
        let status = self.startup_status_now();
        let Self { settings, config, .. } = self;
        if let Some(settings) = settings {
            settings.set_startup_status(Some(status));
            settings.refresh(config);
        }
    }

    fn startup_status_now(&mut self) -> RegistrationStatus {
        if let Some(preview) = &self.preview_startup {
            return preview.clone();
        }
        let (executable, arguments) = startup_invocation();
        let status = self.startup.inspect(self.config.run_on_startup, &executable, &arguments);
        self.startup_status = Some(status.clone());
        status
    }

    fn require_in_sync(status: &RegistrationStatus) -> Result<(), String> {
        if status.in_sync() {
            return Ok(());
        }
        Err(if status.state == RegistrationState::Unreadable {
            status.error.clone().unwrap_or_else(|| "Windows 시작프로그램 상태를 읽을 수 없습니다".to_string())
        } else if status.desired_enabled {
            "Windows 시작프로그램에 현재 실행 파일이 등록되지 않았습니다".to_string()
        } else {
            "Windows 시작프로그램 등록이 제거되지 않았습니다".to_string()
        })
    }

    fn reconcile_startup(&mut self, desired: bool) -> Result<(), String> {
        let (executable, arguments) = startup_invocation();
        let status = self.startup.reconcile(desired, &executable, &arguments)?;
        self.startup_status = Some(status.clone());
        Self::require_in_sync(&status)
    }

    fn synchronize_startup_registration(&mut self) {
        if let Err(error) = self.reconcile_startup(self.config.run_on_startup) {
            log_error!("Unable to register configured startup command: {error}");
            let action = if self.config.run_on_startup {
                "등록하지 못했습니다"
            } else {
                "남아 있는 항목을 정리하지 못했습니다"
            };
            self.defer(
                250,
                Deferred::Toast {
                    message: format!("부팅 시 자동 실행을 {action}: {error}"),
                    kind: ToastKind::Warning,
                    duration_ms: 6000,
                },
            );
        }
    }

    fn rollback_startup(&mut self, desired: bool) {
        if let Err(error) = self.reconcile_startup(desired) {
            log_error!("Unable to roll back startup registry value: {error}");
            self.show_toast(
                &format!("자동 실행 상태를 복구하지 못했습니다. 설정을 다시 시도하거나 앱을 다시 시작해 주세요: {error}"),
                ToastKind::Error,
                7000,
                None,
            );
        }
    }

    pub fn set_startup(&mut self, requested: bool) -> bool {
        let previous = self.config.run_on_startup;
        if let Err(error) = self.reconcile_startup(requested) {
            log_error!("Unable to update startup registry value: {error}");
            self.toast_simple(&format!("자동 실행 설정을 변경하지 못했습니다: {error}"), ToastKind::Error);
            if requested != previous {
                self.rollback_startup(previous);
            }
            return false;
        }
        if self.commit(|config| { config.run_on_startup = requested; Ok(()) }, "자동 실행 설정을 저장하지 못했습니다") {
            return true;
        }
        self.rollback_startup(previous);
        false
    }

    pub fn set_hotkeys(&mut self, panel: &str, quick_add: &str) -> bool {
        let (old_panel, old_quick) = (self.config.hotkey.clone(), self.config.quick_add_hotkey.clone());
        if !self.configure_hotkeys(panel, quick_add, true) {
            return false;
        }
        let registered_panel = self.hotkeys.get(Binding::Panel).unwrap_or(panel).to_string();
        let registered_quick = self.hotkeys.get(Binding::QuickAdd).unwrap_or(quick_add).to_string();
        let saved = self.commit(
            |config| {
                config.hotkey = registered_panel;
                config.quick_add_hotkey = registered_quick;
                Ok(())
            },
            "핫키 설정을 저장하지 못했습니다",
        );
        if saved {
            self.toast_simple("핫키를 변경했습니다.", ToastKind::Success);
            return true;
        }
        // Persistence is authoritative: reapply the previous working pair so
        // native state and the JSON stay in agreement.
        if !self.configure_hotkeys(&old_panel, &old_quick, false) {
            log_error!("Hotkey rollback failed after configuration persistence failure");
            self.show_toast(
                "이전 핫키를 복구하지 못했습니다. 앱을 다시 시작하면 저장된 설정으로 복구됩니다.",
                ToastKind::Error,
                7000,
                None,
            );
        }
        false
    }

    pub fn set_appearance(&mut self, mode: AppearanceMode) {
        if self.commit(|config| { config.appearance_mode = mode; Ok(()) }, "화면 스타일을 저장하지 못했습니다") {
            self.set_palette(palette_for(mode));
        }
    }

    pub fn set_update_checks(&mut self, enabled: bool) {
        let previous = self.config.check_updates;
        if !self.commit(|config| { config.check_updates = enabled; Ok(()) }, "업데이트 확인 설정을 저장하지 못했습니다") {
            return;
        }
        if enabled != previous {
            // Every opt-in period gets its own generation, so a worker started
            // before opt-out stays stale even if the user opts in again.
            self.update_generation += 1;
        }
        if enabled {
            self.check_for_update();
        }
    }

    fn check_for_update(&mut self) {
        if self.stopping || !self.config.check_updates {
            return;
        }
        let generation = self.update_generation;
        if !self.update_inflight.insert(generation) {
            return;
        }
        let spawned = std::thread::Builder::new()
            .name("QuickAccessUpdateCheck".to_string())
            .spawn(move || {
                let result = check_for_update(APP_VERSION, DEFAULT_REPO, &platform::http::fetch);
                post_event(Event::Update { generation, result });
            });
        if let Err(error) = spawned {
            self.update_inflight.remove(&generation);
            log_error!("Unable to start update check worker: {error}");
        }
    }

    fn apply_update_check(&mut self, generation: u64, result: UpdateCheckResult) {
        self.update_inflight.remove(&generation);
        if generation != self.update_generation || !self.config.check_updates || !result.available {
            return;
        }
        let Some(latest) = result.latest_version else {
            return;
        };
        if latest == self.config.last_update_notice {
            return;
        }
        let url = result
            .release_url
            .unwrap_or_else(|| format!("https://github.com/{DEFAULT_REPO}/releases/latest"));
        let notice = latest.clone();
        if !self.commit(|config| { config.last_update_notice = notice; Ok(()) }, "업데이트 확인 상태를 저장하지 못했습니다") {
            return;
        }
        self.show_toast(
            &format!("새 버전 {latest}이(가) 있습니다."),
            ToastKind::Info,
            8000,
            Some(("다운로드 페이지".to_string(), ToastAction::OpenUrl(url))),
        );
    }

    pub fn activate_item(&mut self, item_id: &str) {
        if self.launch_inflight.contains(item_id) {
            return;
        }
        let Some(item) = self.config.get_item(item_id) else {
            return;
        };
        let (name, path) = (item.name.clone(), item.path.clone());
        if !self.launch_slots.try_acquire() {
            self.toast_simple("열기 작업이 많아 잠시 대기 중입니다. 잠시 후 다시 시도해 주세요.", ToastKind::Warning);
            return;
        }
        self.launch_inflight.insert(item_id.to_string());
        let slots = self.launch_slots.clone();
        let id = item_id.to_string();
        // The launched program should come to the foreground, not behind us.
        // SAFETY: AllowSetForegroundWindow takes a plain process id.
        unsafe {
            let _ = AllowSetForegroundWindow(ASFW_ANY);
        }
        let worker_id = id.clone();
        let spawned = std::thread::Builder::new()
            .name("QuickAccessLaunch".to_string())
            .spawn(move || {
                platform::com_init_thread();
                let result = platform::shell_open(&path);
                slots.release();
                post_event(Event::Launch { item_id: worker_id, item_name: name, result });
            });
        if spawned.is_err() {
            self.launch_inflight.remove(&id);
            self.launch_slots.release();
        }
    }

    fn begin_quick_add(&mut self, explorer_hwnd: isize) {
        if self.stopping || self.quick_add_inflight {
            return;
        }
        if !self.quick_add_slots.try_acquire() {
            self.toast_simple("이전 탐색기 응답을 기다리고 있습니다. 잠시 후 다시 시도해 주세요.", ToastKind::Warning);
            return;
        }
        self.quick_add_inflight = true;
        self.quick_add_generation += 1;
        let generation = self.quick_add_generation;
        self.defer(QUICK_ADD_TIMEOUT_MS, Deferred::ExpireQuickAdd(generation));
        let slots = self.quick_add_slots.clone();
        let spawned = std::thread::Builder::new()
            .name("QuickAccessExplorerQuickAdd".to_string())
            .spawn(move || {
                let result = resolve_target(explorer_hwnd);
                post_event(Event::QuickAdd { generation, result });
                // The watchdog only releases the UI wait. COM calls cannot be
                // interrupted, so a timed-out worker keeps its slot until the
                // call returns, bounding retries against a hung shell.
                slots.release();
            });
        if spawned.is_err() {
            self.quick_add_inflight = false;
            self.quick_add_generation += 1;
            self.quick_add_slots.release();
        }
    }

    fn expire_quick_add(&mut self, generation: u64) {
        if generation != self.quick_add_generation || !self.quick_add_inflight {
            return;
        }
        self.quick_add_inflight = false;
        self.quick_add_generation += 1;
        self.toast_simple("탐색기 응답이 지연되어 빠른 등록을 중단했습니다. 다시 시도해 주세요.", ToastKind::Warning);
    }

    fn finish_quick_add(&mut self, generation: u64, result: Result<Vec<ExplorerTarget>, String>) -> Option<Action> {
        if generation != self.quick_add_generation || !self.quick_add_inflight {
            return None;
        }
        self.quick_add_inflight = false;
        self.quick_add_generation += 1;
        match result {
            Err(message) => {
                self.toast_simple(&message, ToastKind::Warning);
                None
            }
            Ok(targets) if targets.len() > 1 => {
                self.add_quick_add_targets(&targets);
                None
            }
            Ok(mut targets) => targets.pop().map(Action::QuickAddOne),
        }
    }

    /// Add a multi-selection with one durable save and one layout refresh.
    fn add_quick_add_targets(&mut self, targets: &[ExplorerTarget]) {
        let mut existing: HashSet<String> = self
            .config
            .items
            .iter()
            .filter(|item| item.item_type != ItemType::Url)
            .map(|item| path_identity(&item.path))
            .collect();
        let mut accepted = Vec::new();
        let mut skipped = 0;
        for target in targets {
            if existing.insert(path_identity(&target.path)) {
                accepted.push(target);
            } else {
                skipped += 1;
            }
        }
        if accepted.is_empty() {
            self.toast_simple("선택한 항목이 모두 이미 등록되어 있습니다.", ToastKind::Warning);
            return;
        }
        let added = RefCell::new(Vec::new());
        let saved = self.commit(
            |config| {
                for target in &accepted {
                    let id = config.add_item(&target.path, &target.suggested_name, Some(target.item_type), None)?;
                    added.borrow_mut().push(id);
                }
                Ok(())
            },
            "선택한 항목을 저장하지 못했습니다",
        );
        if !saved {
            return;
        }
        for id in added.into_inner() {
            self.validate_item(&id);
        }
        let mut message = format!("선택한 항목 {}개를 한 번에 등록했습니다.", accepted.len());
        if skipped > 0 {
            message += &format!(" 이미 등록된 {skipped}개는 건너뛰었습니다.");
        }
        self.toast_simple(&message, ToastKind::Success);
        self.refresh_settings();
    }

    /// Commit an imported config with compensating runtime rollbacks.
    fn apply_imported_config(&mut self, mut candidate: LauncherConfig) -> bool {
        let previous = self.config.clone();
        let hotkeys_changed =
            candidate.hotkey != previous.hotkey || candidate.quick_add_hotkey != previous.quick_add_hotkey;
        let startup_changed = candidate.run_on_startup != previous.run_on_startup;
        let appearance_changed = candidate.appearance_mode != previous.appearance_mode;

        let mut hotkeys_attempted = false;
        let mut startup_attempted = false;
        let mut appearance_applied = false;
        let outcome = (|| -> Result<(), String> {
            if hotkeys_changed {
                hotkeys_attempted = true;
                if !self.configure_hotkeys(&candidate.hotkey.clone(), &candidate.quick_add_hotkey.clone(), true) {
                    return Err("가져온 단축키를 등록할 수 없습니다".to_string());
                }
                if let Some(value) = self.hotkeys.get(Binding::Panel) {
                    candidate.hotkey = value.to_string();
                }
                if let Some(value) = self.hotkeys.get(Binding::QuickAdd) {
                    candidate.quick_add_hotkey = value.to_string();
                }
            }
            if startup_changed {
                startup_attempted = true;
                self.reconcile_startup(candidate.run_on_startup)?;
            }
            if appearance_changed {
                appearance_applied = true;
                self.set_palette(palette_for(candidate.appearance_mode));
            }
            self.store.save(&mut candidate).map_err(|error| error.to_string())
        })();

        if let Err(error) = outcome {
            log_error!("Unable to apply imported configuration: {error}");
            let mut rollback_errors: Vec<String> = Vec::new();
            if appearance_applied {
                self.set_palette(palette_for(previous.appearance_mode));
            }
            if startup_attempted {
                if let Err(rollback) = self.reconcile_startup(previous.run_on_startup) {
                    rollback_errors.push(format!("자동 실행: {rollback}"));
                }
            }
            if hotkeys_attempted && !self.configure_hotkeys(&previous.hotkey, &previous.quick_add_hotkey, false) {
                rollback_errors.push("단축키".to_string());
            }
            let mut detail = format!("설정을 가져오지 못했습니다: {error}");
            if !rollback_errors.is_empty() {
                detail += &format!("\n일부 실행 상태를 복구하지 못했습니다: {}", rollback_errors.join(", "));
            }
            self.show_toast(&detail, ToastKind::Error, 7500, None);
            return false;
        }

        let new_items: HashMap<&str, (&str, ItemType)> = candidate
            .items
            .iter()
            .map(|item| (item.id.as_str(), (item.path.as_str(), item.item_type)))
            .collect();
        let stale: Vec<String> = previous
            .items
            .iter()
            .filter(|old| new_items.get(old.id.as_str()) != Some(&(old.path.as_str(), old.item_type)))
            .map(|old| old.id.clone())
            .collect();
        let updates_changed = candidate.check_updates != previous.check_updates;
        self.config = candidate;
        if updates_changed {
            self.update_generation += 1;
            if self.config.check_updates {
                self.check_for_update();
            }
        }
        for id in stale {
            self.validator.cancel(&id);
            self.statuses.remove(&id);
        }
        self.validate_all_paths();
        self.request_all_icons();
        self.refresh_visible(true);
        true
    }

    fn exit_with(&mut self, code: i32) {
        self.shutdown_services();
        // SAFETY: requesting message-loop exit on this thread.
        unsafe { PostQuitMessage(code) };
    }

    pub fn shutdown(&mut self) {
        if self.stopping {
            return;
        }
        log_info!("QuickAccess shutdown started");
        self.exit_with(0);
    }

    fn shutdown_services(&mut self) {
        self.stopping = true;
        self.validator.close();
        self.icon_service.close();
        let mut api = NativeHotkeys(self.main_hwnd);
        self.hotkeys.clear(&mut api);
        if self.tray_ready {
            tray::remove(self.main_hwnd);
            self.tray_ready = false;
        }
        self.toast.close();
    }

    pub fn owner_window(&self) -> Option<HWND> {
        self.settings.as_ref().filter(|settings| settings.is_visible()).map(|settings| settings.hwnd)
    }

    fn open_settings(&mut self) {
        self.popup.hide(false);
        if self.settings.is_none() {
            match Settings::create() {
                Ok(settings) => self.settings = Some(settings),
                Err(error) => {
                    log_error!("Unable to create the settings window: {error}");
                    self.toast_simple("설정 창을 열지 못했습니다.", ToastKind::Error);
                    return;
                }
            }
        }
        let status = self.startup_status_now();
        let monitor = platform::monitor_at_cursor();
        let palette = self.palette;
        let Self { settings, config, .. } = self;
        if let Some(settings) = settings {
            settings.set_startup_status(Some(status));
            settings.show(config, palette, &monitor);
        }
    }
}

fn owner_and_palette() -> (Option<HWND>, &'static Palette) {
    with_app(|app| (app.owner_window(), app.palette())).unwrap_or((None, &crate::ui::theme::LIGHT))
}

fn web_url_error(value: &str) -> Option<String> {
    normalize_web_url(value)
        .err()
        .map(|_| "http:// 또는 https:// 웹 주소를 입력해 주세요.".to_string())
}

fn choose_and_add(path: Option<String>, item_type: ItemType) {
    let Some(path) = path else { return };
    let (owner, palette) = owner_and_palette();
    if let Some(name) = ask_display_name(owner, palette, &display_name(&path), "표시명 입력") {
        with_app(|app| {
            app.add_item(&path, &name, Some(item_type));
            app.refresh_settings();
        });
    }
}

fn add_link() {
    let (owner, palette) = owner_and_palette();
    let Some(value) = ask_text(owner, palette, "웹 링크 추가", "인터넷 주소를 입력하세요.", "https://", &web_url_error) else {
        return;
    };
    let Ok(url) = normalize_web_url(&value) else { return };
    let hostname = crate::url::split(&url)
        .map(|parts| parts.hostname())
        .ok()
        .filter(|host| !host.is_empty())
        .unwrap_or_else(|| url.clone());
    if let Some(name) = ask_display_name(owner, palette, &hostname, "웹 링크 이름") {
        with_app(|app| {
            app.add_item(&url, &name, Some(ItemType::Url));
            app.refresh_settings();
        });
    }
}

fn edit_item(item_id: &str) {
    let Some(item) = with_app(|app| app.config.get_item(item_id).cloned()).flatten() else {
        return;
    };
    let (owner, palette) = owner_and_palette();
    if item.item_type == ItemType::Url {
        let Some(value) = ask_text(owner, palette, "웹 링크 수정", "인터넷 주소를 입력하세요.", &item.path, &web_url_error) else {
            return;
        };
        let Ok(url) = normalize_web_url(&value) else { return };
        if let Some(name) = ask_display_name(owner, palette, &item.name, "웹 링크 이름") {
            with_app(|app| {
                let saved = app.commit(
                    |config| {
                        config.rename_item(item_id, &name)?;
                        config.replace_path(item_id, &url, Some(ItemType::Url))
                    },
                    "항목을 수정하지 못했습니다",
                );
                if saved {
                    app.statuses.remove(item_id);
                }
                app.refresh_settings();
            });
        }
        return;
    }
    if let Some(name) = ask_display_name(owner, palette, &item.name, "표시명 수정") {
        with_app(|app| {
            app.commit(|config| config.rename_item(item_id, &name), "표시명을 변경하지 못했습니다");
            app.refresh_settings();
        });
    }
}

fn relocate_item(item_id: &str) {
    let Some(item) = with_app(|app| app.config.get_item(item_id).cloned()).flatten() else {
        return;
    };
    let (owner, palette) = owner_and_palette();
    let title = format!("'{}' 경로 재지정", item.name);
    let new_path = match item.item_type {
        ItemType::Url => ask_text(owner, palette, "웹 링크 수정", "새 인터넷 주소를 입력하세요.", &item.path, &web_url_error)
            .and_then(|value| normalize_web_url(&value).ok()),
        ItemType::Folder => dialogs::pick_folder(owner, &title),
        ItemType::File => dialogs::pick_file(owner, &title),
    };
    let Some(new_path) = new_path else { return };
    with_app(|app| {
        let saved = app.commit(
            |config| config.replace_path(item_id, &new_path, Some(item.item_type)),
            "경로를 재지정하지 못했습니다",
        );
        if saved {
            app.validate_item(item_id);
            app.toast_simple("경로를 재지정했습니다.", ToastKind::Success);
        }
        app.refresh_settings();
    });
}

fn format_import_preview(preview: &ImportPreview) -> String {
    let mode = match preview.mode {
        ImportMode::Merge => "기존 설정에 병합",
        ImportMode::Replace => "전체 설정 교체",
    };
    let mut lines = vec![
        format!("방식: {mode}"),
        format!("추가 {}개 · 변경 {}개", preview.added, preview.updated),
        format!("건너뜀 {}개 · 제거 {}개", preview.skipped, preview.removed),
    ];
    if preview.invalid > 0 || preview.conflicts > 0 {
        lines.push(format!(
            "적용 제외: 잘못된 항목 {}개 · 충돌 {}개",
            preview.invalid, preview.conflicts
        ));
    }
    if preview.settings_changed {
        lines.push("단축키·화면·자동 실행 등 앱 설정도 변경됩니다.".to_string());
    }
    lines.join("\n")
}

fn read_bounded(path: &str, limit: u64) -> Result<Vec<u8>, String> {
    use std::io::Read;
    let file = std::fs::File::open(path).map_err(|error| error.to_string())?;
    let mut source = Vec::new();
    file.take(limit + 1)
        .read_to_end(&mut source)
        .map_err(|error| error.to_string())?;
    if source.len() as u64 > limit {
        return Err("가져오기 파일은 2MB 이하여야 합니다".to_string());
    }
    Ok(source)
}

fn import_config() -> bool {
    let (owner, _) = owner_and_palette();
    let toast_error = |message: String| {
        with_app(|app| app.toast_simple(&message, ToastKind::Error));
    };
    let Some(path) = dialogs::pick_json_to_open(owner, "QuickAccess 설정 가져오기") else {
        return false;
    };
    let source = match read_bounded(&path, MAX_IMPORT_BYTES) {
        Ok(source) => source,
        Err(error) => {
            log_error!("Unable to read portable configuration: {error}");
            toast_error(format!("설정 파일을 읽지 못했습니다: {error}"));
            return false;
        }
    };
    let Some(merge) = platform::ask_yes_no_cancel(
        owner,
        "가져오기 방식",
        "기존 설정에 항목을 합칠까요?\n\n예: 현재 설정 유지 + 항목 병합\n아니요: 파일 내용으로 전체 교체\n취소: 가져오기 중단",
    ) else {
        return false;
    };
    let mode = if merge { ImportMode::Merge } else { ImportMode::Replace };
    let Some(current) = with_app(|app| app.config.clone()) else {
        return false;
    };
    let preview = match preview_config_import(&current, &source, mode) {
        Ok(preview) => preview,
        Err(error) => {
            log_error!("Unable to preview portable configuration: {error}");
            toast_error(format!("설정 파일을 분석하지 못했습니다: {error}"));
            return false;
        }
    };
    let summary = format_import_preview(&preview);
    if !preview.changed() {
        platform::show_message(
            owner,
            "가져오기 미리보기",
            &format!("{summary}\n\n적용할 변경 사항이 없습니다."),
            MessageKind::Info,
        );
        return false;
    }
    if !platform::ask_yes_no(owner, "가져오기 미리보기", &format!("{summary}\n\n이 변경 사항을 적용할까요?"), false) {
        return false;
    }
    if preview.mode == ImportMode::Replace
        && (preview.removed > 0 || preview.settings_changed)
        && !platform::ask_yes_no(
            owner,
            "전체 교체 확인",
            "현재 바로가기와 앱 설정이 백업 파일 내용으로 교체됩니다.\n이 작업은 자동으로 되돌릴 수 없습니다. 계속할까요?",
            true,
        )
    {
        return false;
    }
    let applied = with_app(|app| app.apply_imported_config(preview.result)).unwrap_or(false);
    if applied {
        with_app(|app| app.toast_simple("설정을 안전하게 가져왔습니다.", ToastKind::Success));
    }
    applied
}

fn export_config() -> bool {
    let (owner, _) = owner_and_palette();
    let Some(path) = dialogs::pick_json_to_save(owner, "QuickAccess 설정 내보내기", "quickaccess-settings.json") else {
        return false;
    };
    let Some(config) = with_app(|app| app.config.clone()) else {
        return false;
    };
    match write_portable_config(std::path::Path::new(&path), &config) {
        Ok(()) => {
            with_app(|app| app.toast_simple("설정 백업 파일을 저장했습니다.", ToastKind::Success));
            true
        }
        Err(error) => {
            log_error!("Unable to export portable configuration: {error}");
            with_app(|app| app.toast_simple(&format!("설정을 내보내지 못했습니다: {error}"), ToastKind::Error));
            false
        }
    }
}

fn transfer_result(succeeded: bool, success_text: &str) {
    with_app(|app| {
        let note = if succeeded {
            (success_text.to_string(), Tone::Accent)
        } else {
            ("작업이 취소되었거나 적용되지 않았습니다.".to_string(), Tone::Muted)
        };
        if let Some(settings) = app.settings_mut() {
            settings.transfer_note = Some(note);
        }
        app.refresh_settings();
    });
}

fn copy_diagnostics() {
    with_app(|app| {
        let status = app.startup_status_now();
        let report = diagnostics::render(&app.config, Some(status.state.as_str()), &os_description());
        let owner = app.main_hwnd;
        let result = platform::copy_to_clipboard(owner, &report);
        let note = match &result {
            Ok(()) => ("진단 정보를 클립보드에 복사했습니다.".to_string(), Tone::Accent),
            Err(_) => ("진단 정보가 복사되지 않았습니다.".to_string(), Tone::Warning),
        };
        if let Some(settings) = app.settings_mut() {
            settings.diagnostics_note = Some(note);
            settings.invalidate();
        }
        match result {
            Ok(()) => app.toast_simple("개인 경로를 제외한 진단 정보를 복사했습니다.", ToastKind::Success),
            Err(error) => {
                log_error!("Unable to copy diagnostics: {error}");
                app.toast_simple(&format!("진단 정보를 복사하지 못했습니다: {error}"), ToastKind::Error);
            }
        }
    });
}

fn run_action(action: Action) {
    match action {
        Action::OpenSettings => {
            with_app(App::open_settings);
        }
        Action::ActivateItem(id) => {
            with_app(|app| app.activate_item(&id));
        }
        Action::RelocateItem(id) => relocate_item(&id),
        Action::CopyPathMenu { name, path } => {
            let owner = with_app(|app| {
                app.popup.suppress_deactivate = true;
                app.popup.hwnd
            });
            if let Some(owner) = owner {
                run_copy_path_menu(owner, &name, &path);
                with_app(|app| app.popup.suppress_deactivate = false);
            }
        }
        Action::AddFolder => {
            let (owner, _) = owner_and_palette();
            choose_and_add(dialogs::pick_folder(owner, "폴더 선택"), ItemType::Folder);
        }
        Action::AddFile => {
            let (owner, _) = owner_and_palette();
            choose_and_add(dialogs::pick_file(owner, "파일 선택"), ItemType::File);
        }
        Action::AddLink => add_link(),
        Action::EditItem(id) => edit_item(&id),
        Action::DeleteItem(id) => {
            let (owner, _) = owner_and_palette();
            let name = with_app(|app| app.config.get_item(&id).map(|item| item.name.clone())).flatten();
            let Some(name) = name else { return };
            if platform::ask_yes_no(owner, "항목 삭제", &format!("'{name}' 항목을 삭제할까요?"), false) {
                with_app(|app| {
                    app.delete_item(&id);
                    app.refresh_settings();
                });
            }
        }
        Action::MoveItem(id, index) => {
            with_app(|app| {
                app.commit(|config| config.move_item(&id, index), "항목 순서를 변경하지 못했습니다");
                app.refresh_settings();
            });
        }
        Action::SetColumns(columns) => {
            with_app(|app| {
                app.commit(|config| { config.columns = columns; Ok(()) }, "격자 열 수를 저장하지 못했습니다");
                app.refresh_settings();
            });
        }
        Action::SetAppearance(mode) => {
            with_app(|app| {
                app.set_appearance(mode);
                app.refresh_settings();
            });
        }
        Action::SetStartup(enabled) => {
            with_app(|app| {
                app.set_startup(enabled);
                app.refresh_settings();
            });
        }
        Action::SetUpdates(enabled) => {
            with_app(|app| {
                app.set_update_checks(enabled);
                app.refresh_settings();
            });
        }
        Action::ApplyHotkeys(panel, quick) => {
            with_app(|app| {
                if app.set_hotkeys(&panel, &quick) {
                    if let Some(settings) = app.settings_mut() {
                        settings.hotkeys_applied();
                    }
                }
                app.refresh_settings();
            });
        }
        Action::ImportConfig => {
            let succeeded = import_config();
            transfer_result(succeeded, "설정을 가져왔습니다.");
        }
        Action::ExportConfig => {
            let succeeded = export_config();
            transfer_result(succeeded, "설정을 내보냈습니다.");
        }
        Action::CopyDiagnostics => copy_diagnostics(),
        Action::RequestShutdown => {
            let (owner, _) = owner_and_palette();
            if platform::ask_yes_no(owner, "QuickAccess 종료", "QuickAccess를 완전히 종료할까요?", false) {
                with_app(|app| {
                    if let Some(settings) = app.settings_mut() {
                        settings.hide();
                    }
                    app.shutdown();
                });
            }
        }
        Action::RunToastAction(ToastAction::Restore { name, path, item_type, order }) => {
            with_app(|app| app.restore_deleted_item(&name, &path, item_type, order));
        }
        Action::RunToastAction(ToastAction::OpenUrl(url)) => {
            if let Err(error) = platform::shell_open(&url) {
                log_error!("Unable to open the release page: {error}");
                with_app(|app| app.toast_simple(&format!("페이지를 여는 데 실패했습니다: {url}"), ToastKind::Warning));
            }
        }
        Action::QuickAddOne(target) => {
            let (owner, palette) = owner_and_palette();
            if let Some(name) = ask_display_name(owner, palette, &target.suggested_name, "빠른 등록") {
                with_app(|app| {
                    if app.add_item(&target.path, &name, Some(target.item_type)) {
                        app.toast_simple("현재 탐색기 항목을 등록했습니다.", ToastKind::Success);
                        app.refresh_settings();
                    }
                });
            }
        }
        Action::TrayMenu => {
            let Some(owner) = with_app(|app| app.main_hwnd) else { return };
            match tray::show_menu(owner) {
                Some(tray::MENU_OPEN_PANEL) => {
                    with_app(App::open_panel);
                }
                Some(tray::MENU_OPEN_SETTINGS) => {
                    with_app(App::open_settings);
                }
                Some(tray::MENU_QUIT) => {
                    with_app(App::shutdown);
                }
                _ => {}
            }
        }
    }
}

// ---- Developer entry points -------------------------------------------------

/// Pump messages for a short while so windows can settle during dev tooling.
pub fn pump_for(milliseconds: u64) {
    use windows::Win32::UI::WindowsAndMessaging::{DispatchMessageW, PeekMessageW, TranslateMessage, MSG, PM_REMOVE};
    let deadline = Instant::now() + Duration::from_millis(milliseconds);
    let mut message = MSG::default();
    loop {
        // SAFETY: the standard message pump on this thread.
        unsafe {
            while PeekMessageW(&mut message, None, 0, 0, PM_REMOVE).as_bool() {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
        if Instant::now() >= deadline {
            return;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}

pub mod dev {
    use super::*;
    use crate::ui::capture;
    use crate::ui::settings::Page;

    fn open_settings_page(page: Page) {
        with_app(|app| {
            app.open_settings();
            if let Some(settings) = app.settings_mut() {
                settings.select_page(page);
            }
        });
    }

    /// Capture real windows to PNG files for visual review.
    pub fn capture_all(directory: &std::path::Path) -> Result<(), String> {
        std::fs::create_dir_all(directory).map_err(|error| error.to_string())?;
        pump_for(900);
        // Registration used the isolated F23/F24 keys; screenshots show the
        // shipped defaults. Only the in-memory copy changes.
        with_app(|app| {
            app.config.hotkey = crate::model::DEFAULT_HOTKEY.to_string();
            app.config.quick_add_hotkey = crate::model::DEFAULT_QUICK_ADD_HOTKEY.to_string();
            // The default is on; the isolated run turned it off. Show the state
            // a normal installation has instead of this machine's Run value.
            app.config.run_on_startup = true;
            app.preview_startup = Some(RegistrationStatus {
                state: crate::startup_rules::RegistrationState::Correct,
                desired_enabled: true,
                expected_command: String::new(),
                actual_command: None,
                error: None,
            });
        });
        for (mode, suffix) in [(AppearanceMode::Light, "light"), (AppearanceMode::Dark, "dark")] {
            with_app(|app| app.set_appearance(mode));
            with_app(App::open_panel);
            pump_for(350);
            let popup = with_app(|app| app.popup.hwnd).ok_or("no app")?;
            capture::save_window(popup, &directory.join(format!("popup-{suffix}.png")))?;
            with_app(|app| {
                crate::ui::kit::set_edit_text(app.popup.edit, "ㅁㅅ");
                app.popup.on_search_changed();
            });
            pump_for(150);
            capture::save_window(popup, &directory.join(format!("popup-search-{suffix}.png")))?;
            with_app(|app| {
                crate::ui::kit::set_edit_text(app.popup.edit, "없는항목");
                app.popup.on_search_changed();
            });
            pump_for(150);
            capture::save_window(popup, &directory.join(format!("popup-empty-{suffix}.png")))?;
            with_app(|app| app.popup.hide(true));

            for (page, name) in [
                (Page::Items, "items"),
                (Page::Shortcuts, "shortcuts"),
                (Page::Appearance, "appearance"),
                (Page::About, "about"),
            ] {
                open_settings_page(page);
                with_app(App::refresh_settings);
                pump_for(250);
                let window = with_app(|app| app.settings_mut().map(|settings| settings.hwnd))
                    .flatten()
                    .ok_or("no settings window")?;
                capture::save_window(window, &directory.join(format!("settings-{name}-{suffix}.png")))?;
            }
            with_app(|app| {
                if let Some(settings) = app.settings_mut() {
                    settings.hide();
                }
            });

            with_app(|app| {
                app.show_toast(
                    "'프로젝트 문서' 항목을 삭제했습니다.",
                    ToastKind::Warning,
                    6000,
                    Some((
                        "실행취소".to_string(),
                        ToastAction::OpenUrl("https://example.invalid".to_string()),
                    )),
                );
            });
            pump_for(200);
            let toast = with_app(|app| app.toast.hwnd).ok_or("no toast")?;
            capture::save_window(toast, &directory.join(format!("toast-{suffix}.png")))?;
            with_app(|app| app.toast.close());
        }
        Ok(())
    }

    fn percentile(sorted: &[f64], fraction: f64) -> f64 {
        if sorted.is_empty() {
            return 0.0;
        }
        let rank = (fraction * sorted.len() as f64).ceil() as usize;
        sorted[rank.clamp(1, sorted.len()) - 1]
    }

    fn summarize(mut samples: Vec<f64>) -> serde_json::Value {
        samples.sort_by(f64::total_cmp);
        serde_json::json!({
            "count": samples.len(),
            "median": percentile(&samples, 0.5),
            "p95": percentile(&samples, 0.95),
            "max": samples.last().copied().unwrap_or(0.0),
        })
    }

    /// Measure the hotkey-to-visible path with the release app logic.
    pub fn benchmark(output: &std::path::Path, ready_ms: f64, items: usize) -> Result<(), String> {
        pump_for(500);
        let mut show_calls = Vec::new();
        let mut command_to_visible = Vec::new();
        for round in 0..35 {
            let started = Instant::now();
            with_app(App::open_panel);
            let elapsed = started.elapsed().as_secs_f64() * 1000.0;
            pump_for(10);
            with_app(|app| app.popup.hide(true));
            pump_for(10);
            if round >= 5 {
                show_calls.push(elapsed);
            }
        }
        let panel_id = 0x5100;
        let main = with_app(|app| app.main_hwnd).ok_or("no app")?;
        for round in 0..35 {
            let started = Instant::now();
            // SAFETY: posting the same message a real hotkey press delivers.
            unsafe {
                PostMessageW(Some(main), WM_HOTKEY, WPARAM(panel_id), LPARAM(0)).map_err(|error| error.to_string())?;
            }
            let visible_at = loop {
                pump_for(0);
                if with_app(|app| app.popup.visible).unwrap_or(false) {
                    break started.elapsed().as_secs_f64() * 1000.0;
                }
                if started.elapsed() > Duration::from_secs(5) {
                    return Err("popup did not open".to_string());
                }
            };
            pump_for(10);
            with_app(|app| app.popup.hide(true));
            pump_for(10);
            if round >= 5 {
                command_to_visible.push(visible_at);
            }
        }
        let report = serde_json::json!({
            "implementation": "rust",
            "items": items,
            "resident_ready_ms": ready_ms,
            "popup_warm_show_call_ms": summarize(show_calls),
            "open_panel_command_to_visible_ms": summarize(command_to_visible),
        });
        std::fs::write(output, crate::storage::render_json(&report)).map_err(|error| error.to_string())
    }
}
