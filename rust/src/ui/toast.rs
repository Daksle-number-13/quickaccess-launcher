//! One compact, non-activating toast with a semantic status accent.

use windows::core::w;
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::UI::Controls::WM_MOUSELEAVE;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, LoadCursorW, RegisterClassW, SetWindowPos,
    ShowWindow, CS_HREDRAW, CS_VREDRAW, HWND_TOPMOST, IDC_ARROW, MA_NOACTIVATE, SWP_NOACTIVATE,
    SW_HIDE, SW_SHOWNOACTIVATE, WM_ERASEBKGND, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEACTIVATE,
    WM_MOUSEMOVE, WM_PAINT, WNDCLASSW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
    WS_POPUP,
};

use crate::app::{dispatch, with_app, Action};
use crate::model::ItemType;
use crate::platform::MonitorContext;
use crate::ui::gfx::{gfx, Surface, TextStyle, R};
use crate::ui::kit::{
    apply_round_region, apply_window_chrome, button, guard_panic, pointer, run_paint, ButtonStyle, Kit, Wid,
};
use crate::ui::theme::{Color, Palette};

const W_ACTION: Wid = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToastKind {
    Info,
    Success,
    Warning,
    Error,
}

impl ToastKind {
    fn style(self, palette: &Palette) -> (Color, &'static str) {
        match self {
            Self::Info => (palette.accent, "i"),
            Self::Success => (palette.success, "✓"),
            Self::Warning => (palette.warning, "!"),
            Self::Error => (palette.danger, "×"),
        }
    }
}

/// What the optional toast button does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToastAction {
    Restore {
        name: String,
        path: String,
        item_type: ItemType,
        order: i64,
    },
    OpenUrl(String),
}

pub struct Toast {
    pub hwnd: HWND,
    surface: Surface,
    kit: Kit,
    message: String,
    kind: ToastKind,
    action_label: Option<String>,
    pub action: Option<ToastAction>,
    width: f32,
    height: f32,
    dpi: u32,
    rounded: bool,
}

fn register_class(instance: HINSTANCE) {
    let class = WNDCLASSW {
        style: CS_HREDRAW | CS_VREDRAW,
        lpfnWndProc: Some(toast_proc),
        hInstance: instance,
        lpszClassName: w!("QuickAccessToast"),
        // SAFETY: loading a stock system cursor.
        hCursor: unsafe { LoadCursorW(None, IDC_ARROW) }.unwrap_or_default(),
        ..Default::default()
    };
    // SAFETY: the class struct refers to static strings and a valid procedure.
    unsafe { RegisterClassW(&class) };
}

/// The toast's logical size for a message. Wide enough for 42 Korean characters.
pub fn toast_size(graphics_text_height: f32, has_action: bool, work_width: f32, work_height: f32, scale: f32) -> (f32, f32) {
    let base_width: f32 = if has_action { 460.0 } else { 420.0 };
    let width = base_width.min(((work_width - 24.0) / scale).max(180.0));
    let height = (graphics_text_height + 28.0).clamp(64.0, 180.0).min(((work_height - 24.0) / scale).max(56.0));
    (width, height)
}

impl Toast {
    pub fn create() -> windows::core::Result<Self> {
        // SAFETY: standard window creation on the UI thread; the handle lives
        // for the process lifetime.
        let hwnd = unsafe {
            let instance: HINSTANCE = GetModuleHandleW(None)?.into();
            register_class(instance);
            CreateWindowExW(
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                w!("QuickAccessToast"),
                w!("QuickAccess"),
                WS_POPUP,
                0,
                0,
                420,
                64,
                None,
                None,
                Some(instance),
                None,
            )?
        };
        Ok(Self {
            hwnd,
            surface: Surface::new(hwnd, 96),
            kit: Kit::default(),
            message: String::new(),
            kind: ToastKind::Info,
            action_label: None,
            action: None,
            width: 420.0,
            height: 64.0,
            dpi: 96,
            rounded: false,
        })
    }

    fn scale(&self) -> f32 {
        self.dpi as f32 / 96.0
    }

    fn text_width(&self) -> f32 {
        self.width - if self.action_label.is_some() { 150.0 } else { 90.0 }
    }

    pub fn show(
        &mut self,
        message: &str,
        kind: ToastKind,
        action: Option<(String, ToastAction)>,
        palette: &Palette,
        monitor: &MonitorContext,
    ) {
        self.message = message.to_string();
        self.kind = kind;
        (self.action_label, self.action) = match action {
            Some((label, action)) => (Some(label), Some(action)),
            None => (None, None),
        };
        self.dpi = monitor.dpi;
        let scale = self.scale();
        let work = monitor.work_area;
        let has_action = self.action_label.is_some();
        let (width, _) = toast_size(0.0, has_action, work.width() as f32, work.height() as f32, scale);
        self.width = width;
        let text_height = gfx()
            .map(|graphics| graphics.measure(message, 11.0, false, self.text_width(), true).1)
            .unwrap_or(36.0);
        (self.width, self.height) =
            toast_size(text_height, has_action, work.width() as f32, work.height() as f32, scale);

        let pixel_width = (self.width * scale).round() as i32;
        let pixel_height = (self.height * scale).round() as i32;
        let x = (work.left + 12).max(work.right - pixel_width - 18);
        let y = (work.top + 12).max(work.bottom - pixel_height - 18);
        self.rounded = apply_window_chrome(self.hwnd, palette, true);
        // SAFETY: moving a window owned by this thread before it is revealed,
        // so the toast never visibly jumps between monitors.
        unsafe {
            let _ = SetWindowPos(self.hwnd, Some(HWND_TOPMOST), x, y, pixel_width, pixel_height, SWP_NOACTIVATE);
        }
        if !self.rounded {
            apply_round_region(self.hwnd, pixel_width, pixel_height, (12.0 * scale) as i32);
        }
        self.surface.set_dpi(self.dpi);
        self.surface.sync_size();
        self.paint(palette);
        // SAFETY: showing without taking focus from the user's current window.
        unsafe {
            let _ = ShowWindow(self.hwnd, SW_SHOWNOACTIVATE);
            let _ = InvalidateRect(Some(self.hwnd), None, false);
        }
    }

    pub fn close(&mut self) {
        self.action = None;
        // SAFETY: hiding a window owned by this thread.
        unsafe {
            let _ = ShowWindow(self.hwnd, SW_HIDE);
        }
    }

    pub fn paint(&mut self, palette: &Palette) {
        let (color, symbol) = self.kind.style(palette);
        let (width, height) = (self.width, self.height);
        let text_width = self.text_width();
        let message = self.message.clone();
        let label = self.action_label.clone();
        let rounded = self.rounded;
        let kit = &mut self.kit;
        self.surface.draw(|painter| {
            kit.begin_frame();
            painter.clear(palette.surface);
            if !rounded {
                painter.stroke(R::new(0.0, 0.0, width, height), 12.0, 1.0, palette.border);
            }
            painter.fill(R::new(4.0, 5.0, 4.0, height - 10.0), 2.0, color);
            let badge = R::new(20.0, (height - 26.0) / 2.0, 26.0, 26.0);
            painter.fill(badge, 13.0, palette.surface_alt);
            painter.text(badge, symbol, TextStyle::new(13.0, true, color).centered());
            let measured = painter.graphics.measure(&message, 11.0, false, text_width, true).1;
            let text = R::new(56.0, ((height - measured) / 2.0).max(6.0), text_width, measured.min(height - 12.0));
            painter.text(text, &message, TextStyle::new(11.0, false, palette.text).wrapped());
            if let Some(label) = &label {
                let rect = R::new(width - 14.0 - 76.0, (height - 30.0) / 2.0, 76.0, 30.0);
                let style = ButtonStyle {
                    fill: Some(color),
                    hover: color,
                    text: if palette.dark { palette.bg } else { palette.white },
                    ..ButtonStyle::primary(palette)
                }
                .sized(10.0)
                .radius(8.0);
                button(painter, kit, palette, W_ACTION, rect, label, style, true);
            }
        });
    }

    fn pointer_moved(&mut self, lparam: LPARAM, palette: &Palette) {
        let (x, y) = pointer(lparam, self.scale());
        if self.kit.mouse_move(self.hwnd, x, y) {
            self.paint(palette);
        }
    }

    fn pointer_left(&mut self, palette: &Palette) {
        self.kit.mouse_leave();
        self.paint(palette);
    }

    fn pressed(&mut self, lparam: LPARAM) {
        let (x, y) = pointer(lparam, self.scale());
        self.kit.mouse_down(x, y);
    }

    fn released(&mut self, lparam: LPARAM) -> Option<ToastAction> {
        let (x, y) = pointer(lparam, self.scale());
        if self.kit.mouse_up(x, y) == Some(W_ACTION) {
            let action = self.action.take();
            self.close();
            return action;
        }
        None
    }
}

unsafe extern "system" fn toast_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    guard_panic(LRESULT(0), || match message {
        WM_ERASEBKGND => LRESULT(1),
        WM_MOUSEACTIVATE => LRESULT(MA_NOACTIVATE as isize),
        WM_PAINT => run_paint(hwnd, || {
            with_app(|app| app.paint_toast());
        }),
        WM_MOUSEMOVE => {
            with_app(|app| {
                let palette = app.palette();
                app.toast.pointer_moved(lparam, palette);
            });
            LRESULT(0)
        }
        WM_MOUSELEAVE => {
            with_app(|app| {
                let palette = app.palette();
                app.toast.pointer_left(palette);
            });
            LRESULT(0)
        }
        WM_LBUTTONDOWN => {
            with_app(|app| app.toast.pressed(lparam));
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            dispatch(|app| {
                app.toast
                    .released(lparam)
                    .map(Action::RunToastAction)
                    .into_iter()
                    .collect()
            });
            LRESULT(0)
        }
        // SAFETY: forwarding unmodified parameters for this window.
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_to_the_message_within_bounds() {
        assert_eq!(toast_size(16.0, false, 1920.0, 1040.0, 1.0), (420.0, 64.0));
        assert_eq!(toast_size(80.0, true, 1920.0, 1040.0, 1.0), (460.0, 108.0));
        assert_eq!(toast_size(800.0, false, 1920.0, 1040.0, 1.0).1, 180.0);
        let (width, height) = toast_size(800.0, true, 300.0, 100.0, 2.0);
        assert_eq!(width, 180.0);
        assert_eq!(height, 56.0);
    }
}
