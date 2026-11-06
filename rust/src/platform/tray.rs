//! Notification-area icon owned by the main window's message loop.
//!
//! Running on the UI thread means no extra thread or message pump. Windows'
//! `TaskbarCreated` broadcast restores the icon after Explorer restarts.

use windows::core::PCWSTR;
use windows::Win32::Foundation::{HWND, POINT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NOTIFYICONDATAW,
};
use windows::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreatePopupMenu, DestroyMenu, GetCursorPos, GetSystemMetrics, LoadImageW,
    PostMessageW, RegisterWindowMessageW, SetForegroundWindow, TrackPopupMenu, HICON, IMAGE_ICON,
    LR_DEFAULTCOLOR, MF_SEPARATOR, MF_STRING, SM_CXSMICON, SM_CYSMICON, TPM_RETURNCMD,
    TPM_RIGHTBUTTON, WM_NULL,
};

use crate::pyish::wide;

const TRAY_ID: u32 = 1;
pub const MENU_OPEN_PANEL: i32 = 1;
pub const MENU_OPEN_SETTINGS: i32 = 2;
pub const MENU_QUIT: i32 = 3;

pub fn app_icon(width: i32, height: i32) -> Option<HICON> {
    // SAFETY: resource id 1 is embedded by build.rs; the module handle is this
    // executable, valid for the process lifetime.
    unsafe {
        let module = GetModuleHandleW(None).ok()?;
        let handle = LoadImageW(
            Some(module.into()),
            PCWSTR(1 as *const u16),
            IMAGE_ICON,
            width,
            height,
            LR_DEFAULTCOLOR,
        )
        .ok()?;
        Some(HICON(handle.0))
    }
}

pub fn taskbar_created_message() -> u32 {
    let name = wide("TaskbarCreated");
    // SAFETY: the name is NUL-terminated and outlives the call.
    unsafe { RegisterWindowMessageW(PCWSTR(name.as_ptr())) }
}

fn notify_data(owner: HWND) -> NOTIFYICONDATAW {
    NOTIFYICONDATAW {
        cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
        hWnd: owner,
        uID: TRAY_ID,
        ..Default::default()
    }
}

/// Add the icon. Fails while Explorer is still starting; callers retry.
pub fn add(owner: HWND, callback_message: u32, title: &str) -> Result<(), String> {
    // SAFETY: GetSystemMetrics has no preconditions.
    let (width, height) = unsafe { (GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON)) };
    let icon = app_icon(width, height).ok_or("트레이 아이콘을 불러오지 못했습니다")?;
    let mut data = notify_data(owner);
    data.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    data.uCallbackMessage = callback_message;
    data.hIcon = icon;
    for (slot, unit) in data.szTip.iter_mut().zip(title.encode_utf16().take(127)) {
        *slot = unit;
    }
    // SAFETY: `data` is fully initialized with its correct cbSize.
    if unsafe { Shell_NotifyIconW(NIM_ADD, &data) }.as_bool() {
        Ok(())
    } else {
        Err("Shell_NotifyIcon failed".to_string())
    }
}

pub fn remove(owner: HWND) {
    let data = notify_data(owner);
    // SAFETY: `data` identifies the icon by owner and id.
    unsafe {
        let _ = Shell_NotifyIconW(NIM_DELETE, &data);
    }
}

/// Run the tray menu modally and return the selected command, if any.
pub fn show_menu(owner: HWND) -> Option<i32> {
    let open = wide("패널 열기");
    let settings = wide("설정");
    let quit = wide("종료");
    // SAFETY: labels outlive the calls; the menu is destroyed below. The
    // foreground and WM_NULL steps are the documented way to make a
    // notification menu dismiss correctly.
    unsafe {
        let menu = CreatePopupMenu().ok()?;
        let _ = AppendMenuW(menu, MF_STRING, MENU_OPEN_PANEL as usize, PCWSTR(open.as_ptr()));
        let _ = AppendMenuW(menu, MF_STRING, MENU_OPEN_SETTINGS as usize, PCWSTR(settings.as_ptr()));
        let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
        let _ = AppendMenuW(menu, MF_STRING, MENU_QUIT as usize, PCWSTR(quit.as_ptr()));
        let mut point = POINT::default();
        let _ = GetCursorPos(&mut point);
        let _ = SetForegroundWindow(owner);
        let command = TrackPopupMenu(
            menu,
            TPM_RETURNCMD | TPM_RIGHTBUTTON,
            point.x,
            point.y,
            None,
            owner,
            None,
        );
        let _ = PostMessageW(Some(owner), WM_NULL, WPARAM(0), Default::default());
        let _ = DestroyMenu(menu);
        (command.0 != 0).then_some(command.0)
    }
}
