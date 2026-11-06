//! Thin wrappers over the Win32 APIs the launcher depends on.

pub mod dialogs;
pub mod explorer;
pub mod http;
pub mod native_icons;
pub mod registry;
pub mod singleton;
pub mod tray;

use windows::core::{GUID, PCWSTR, PWSTR};
use windows::Win32::Foundation::{GlobalFree, HANDLE, HGLOBAL, HWND, POINT, RECT};
use windows::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromPoint, HMONITOR, MONITORINFO,
    MONITORINFOEXW, MONITOR_DEFAULTTONEAREST,
};
use windows::Win32::System::Com::{CoInitializeEx, CoTaskMemFree, COINIT_APARTMENTTHREADED};
use windows::Win32::System::DataExchange::{
    CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData,
};
use windows::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
use windows::Win32::System::SystemInformation::GetLocalTime;
use windows::Win32::UI::HiDpi::{
    GetDpiForMonitor, SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    MDT_EFFECTIVE_DPI,
};
use windows::Win32::UI::Shell::{
    FOLDERID_Documents, FOLDERID_Downloads, SHGetKnownFolderPath, ShellExecuteW, KF_FLAG_DEFAULT,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, GetForegroundWindow, MessageBoxW, MB_ICONERROR, MB_ICONINFORMATION,
    MB_ICONWARNING, MB_OK, MB_SETFOREGROUND, MB_YESNO, MB_YESNOCANCEL, SW_SHOWNORMAL, IDCANCEL,
    IDNO, IDYES,
};

use crate::geometry::{Point, Rect};
use crate::model::DefaultFolders;
use crate::pyish::wide;

/// Initialize COM on a worker that calls shell APIs. Failure is tolerated
/// because shell calls then simply report no result.
pub fn com_init_thread() {
    // SAFETY: called once at the start of a thread this crate owns; the
    // apartment lives until the thread or process ends.
    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }
}

pub fn local_timestamp() -> String {
    // SAFETY: GetLocalTime only fills the returned struct.
    let now = unsafe { GetLocalTime() };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond
    )
}

pub fn enable_dpi_awareness() {
    // The manifest already requests this; the call covers unusual loaders.
    // SAFETY: no pointers are involved, and failure just means already set.
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
}

fn known_folder(id: &GUID) -> Option<String> {
    // SAFETY: the returned buffer is copied, then freed with CoTaskMemFree as
    // the API requires.
    unsafe {
        let path: PWSTR = SHGetKnownFolderPath(id, KF_FLAG_DEFAULT, None).ok()?;
        let text = path.to_string().ok();
        CoTaskMemFree(Some(path.0 as *const _));
        text.filter(|value| !value.is_empty())
    }
}

/// Redirected Downloads and Documents locations, with safe fallbacks.
pub fn default_folders() -> DefaultFolders {
    let home = std::env::var("USERPROFILE").unwrap_or_else(|_| ".".to_string());
    let fallback = DefaultFolders::under(&home);
    DefaultFolders {
        downloads: known_folder(&FOLDERID_Downloads).unwrap_or(fallback.downloads),
        documents: known_folder(&FOLDERID_Documents).unwrap_or(fallback.documents),
    }
}

pub fn foreground_window() -> isize {
    // SAFETY: GetForegroundWindow has no preconditions.
    unsafe { GetForegroundWindow().0 as isize }
}

/// Display details needed before a popup is made visible.
#[derive(Debug, Clone, PartialEq)]
pub struct MonitorContext {
    pub identifier: String,
    pub bounds: Rect,
    pub work_area: Rect,
    pub dpi: u32,
}

impl MonitorContext {
    pub fn fallback() -> Self {
        let area = Rect { left: 0, top: 0, right: 1280, bottom: 720 };
        Self { identifier: "fallback-primary".to_string(), bounds: area, work_area: area, dpi: 96 }
    }
}

pub fn cursor_position() -> Option<Point> {
    let mut point = POINT::default();
    // SAFETY: `point` is a valid out-parameter for the call's duration.
    unsafe { GetCursorPos(&mut point) }.ok()?;
    Some(Point { x: point.x, y: point.y })
}

fn from_rect(rect: RECT) -> Rect {
    Rect { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
}

fn context_for(monitor: HMONITOR) -> Option<MonitorContext> {
    let mut info = MONITORINFOEXW::default();
    info.monitorInfo.cbSize = std::mem::size_of::<MONITORINFOEXW>() as u32;
    // SAFETY: cbSize declares the extended struct passed by pointer.
    let ok = unsafe { GetMonitorInfoW(monitor, &mut info as *mut _ as *mut MONITORINFO) }.as_bool();
    if !ok {
        return None;
    }
    let (mut dpi_x, mut dpi_y) = (96_u32, 96_u32);
    // SAFETY: both out-parameters are valid; a failure keeps 96 DPI.
    if unsafe { GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &mut dpi_x, &mut dpi_y) }.is_err() {
        dpi_x = 96;
    }
    Some(MonitorContext {
        identifier: String::from_utf16_lossy(&info.szDevice)
            .trim_end_matches('\0')
            .to_string(),
        bounds: from_rect(info.monitorInfo.rcMonitor),
        work_area: from_rect(info.monitorInfo.rcWork),
        dpi: dpi_x.max(1),
    })
}

/// Work area and DPI for the monitor nearest `point`.
pub fn monitor_at(point: Point) -> Option<MonitorContext> {
    // SAFETY: MonitorFromPoint takes the point by value.
    let monitor = unsafe {
        MonitorFromPoint(POINT { x: point.x, y: point.y }, MONITOR_DEFAULTTONEAREST)
    };
    context_for(monitor)
}

pub fn monitor_at_cursor() -> MonitorContext {
    cursor_position()
        .and_then(monitor_at)
        .unwrap_or_else(MonitorContext::fallback)
}

/// Open a path or URL with its registered handler.
pub fn shell_open(target: &str) -> Result<(), String> {
    if target.is_empty() || target.contains('\0') {
        return Err("path must be non-empty and must not contain NUL".to_string());
    }
    let wide_target = wide(target);
    let verb = wide("open");
    // SAFETY: both strings are NUL-terminated and outlive the call.
    let result = unsafe {
        ShellExecuteW(
            None,
            PCWSTR(verb.as_ptr()),
            PCWSTR(wide_target.as_ptr()),
            PCWSTR::null(),
            PCWSTR::null(),
            SW_SHOWNORMAL,
        )
    };
    let code = result.0 as isize;
    if code > 32 {
        Ok(())
    } else {
        Err(shell_error_text(code))
    }
}

fn shell_error_text(code: isize) -> String {
    match code {
        0 | 8 => "메모리가 부족합니다".to_string(),
        2 => "파일을 찾을 수 없습니다".to_string(),
        3 => "경로를 찾을 수 없습니다".to_string(),
        5 => "액세스가 거부되었습니다".to_string(),
        26 | 32 => "공유 위반이 발생했습니다".to_string(),
        27 | 31 => "이 파일 형식에 연결된 프로그램이 없습니다".to_string(),
        other => format!("Windows 오류 {other}"),
    }
}

pub fn copy_to_clipboard(owner: HWND, text: &str) -> Result<(), String> {
    const CF_UNICODETEXT: u32 = 13;
    let data = wide(text);
    let bytes = data.len() * 2;
    // SAFETY: the buffer is allocated with `bytes` capacity, written while
    // locked, and ownership passes to the clipboard on success.
    unsafe {
        OpenClipboard(Some(owner)).map_err(|error| error.to_string())?;
        let result = (|| -> windows::core::Result<()> {
            EmptyClipboard()?;
            let memory: HGLOBAL = GlobalAlloc(GMEM_MOVEABLE, bytes)?;
            let target = GlobalLock(memory) as *mut u16;
            if target.is_null() {
                let _ = GlobalFree(Some(memory));
                return Err(windows::core::Error::from_win32());
            }
            std::ptr::copy_nonoverlapping(data.as_ptr(), target, data.len());
            let _ = GlobalUnlock(memory);
            if let Err(error) = SetClipboardData(CF_UNICODETEXT, Some(HANDLE(memory.0))) {
                let _ = GlobalFree(Some(memory));
                return Err(error);
            }
            Ok(())
        })();
        let _ = CloseClipboard();
        result.map_err(|error| error.to_string())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageKind {
    Info,
    Error,
}

fn message_box(owner: Option<HWND>, title: &str, text: &str, flags: u32) -> i32 {
    let title = wide(title);
    let text = wide(text);
    // SAFETY: both strings are NUL-terminated and outlive the modal call.
    unsafe {
        MessageBoxW(
            owner,
            PCWSTR(text.as_ptr()),
            PCWSTR(title.as_ptr()),
            windows::Win32::UI::WindowsAndMessaging::MESSAGEBOX_STYLE(flags),
        )
        .0
    }
}

pub fn show_message(owner: Option<HWND>, title: &str, text: &str, kind: MessageKind) {
    let icon = match kind {
        MessageKind::Info => MB_ICONINFORMATION,
        MessageKind::Error => MB_ICONERROR,
    };
    message_box(owner, title, text, (MB_OK | icon | MB_SETFOREGROUND).0);
}

pub fn ask_yes_no(owner: Option<HWND>, title: &str, text: &str, warning: bool) -> bool {
    let icon = if warning { MB_ICONWARNING } else { MB_ICONINFORMATION };
    message_box(owner, title, text, (MB_YESNO | icon | MB_SETFOREGROUND).0) == IDYES.0
}

/// `Some(true)` yes, `Some(false)` no, `None` cancel.
pub fn ask_yes_no_cancel(owner: Option<HWND>, title: &str, text: &str) -> Option<bool> {
    let flags = (MB_YESNOCANCEL | MB_ICONINFORMATION | MB_SETFOREGROUND).0;
    let answer = message_box(owner, title, text, flags);
    if answer == IDCANCEL.0 {
        None
    } else if answer == IDNO.0 {
        Some(false)
    } else {
        Some(true)
    }
}

pub fn fatal_message(text: &str) {
    show_message(None, "QuickAccess", text, MessageKind::Error);
}
