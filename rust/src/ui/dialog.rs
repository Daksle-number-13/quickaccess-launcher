//! Modal text input dialog with its own nested message loop.
//!
//! Call only from outside any application-state borrow: the nested loop keeps
//! dispatching messages to every other window while the dialog is open.

use std::cell::RefCell;

use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Controls::WM_MOUSELEAVE;
use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
use windows::Win32::UI::HiDpi::{AdjustWindowRectExForDpi, GetDpiForWindow};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    EnableWindow, GetKeyState, SetFocus, VK_ESCAPE, VK_RETURN, VK_SHIFT, VK_SPACE, VK_TAB,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetMessageW, GetParent,
    GetWindowLongPtrW, GetWindowRect, IsWindowVisible, LoadCursorW, PostQuitMessage,
    RegisterClassW, SetForegroundWindow, SetWindowLongPtrW, SetWindowPos, ShowWindow, TranslateMessage,
    ES_AUTOHSCROLL, GWLP_USERDATA, HMENU, HWND_TOP, HWND_TOPMOST, IDC_ARROW, MSG, SWP_NOACTIVATE,
    SWP_NOZORDER, SW_SHOW, WINDOW_EX_STYLE, WINDOW_STYLE, WM_CHAR, WM_CLOSE, WM_CTLCOLOREDIT, WM_ERASEBKGND,
    WM_KEYDOWN, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_PAINT, WNDCLASSW, WS_CAPTION,
    WS_CHILD, WS_EX_DLGMODALFRAME, WS_SYSMENU, WS_VISIBLE,
};

use crate::geometry::{center_in_work_area, clamp_to_work_area, Point, Size};
use crate::platform::{monitor_at, monitor_at_cursor, MonitorContext};
use crate::pyish::{strip, wide};
use crate::ui::gfx::{Surface, TextStyle, R};
use crate::ui::kit::{
    apply_window_chrome, button, edit_colors, edit_text, guard_panic, physical, pointer, run_paint,
    select_all, set_edit_palette, set_edit_text, ButtonStyle, EditFont, Kit, Wid,
};
use crate::ui::theme::Palette;

const W_CANCEL: Wid = 1;
const W_CONFIRM: Wid = 2;
const W_FIELD: Wid = 3;
const WIDTH: f32 = 458.0;
const HEIGHT: f32 = 216.0;
const ERROR_EXTRA: f32 = 24.0;

type Validator<'a> = &'a dyn Fn(&str) -> Option<String>;

struct DialogState<'a> {
    hwnd: HWND,
    edit: HWND,
    surface: Surface,
    kit: Kit,
    palette: &'static Palette,
    prompt: String,
    validator: Validator<'a>,
    error: Option<String>,
    result: Option<String>,
    done: bool,
    dpi: u32,
    _font: Option<EditFont>,
    edit_placed: bool,
}

impl DialogState<'_> {
    fn scale(&self) -> f32 {
        self.dpi as f32 / 96.0
    }

    fn logical_height(&self) -> f32 {
        HEIGHT + if self.error.is_some() { ERROR_EXTRA } else { 0.0 }
    }

    fn invalidate(&self) {
        // SAFETY: invalidating a window owned by this thread.
        unsafe {
            let _ = InvalidateRect(Some(self.hwnd), None, false);
        }
    }

    fn field_rect(&self) -> R {
        R::new(34.0, 90.0, WIDTH - 68.0, 40.0)
    }

    fn paint(&mut self) {
        let palette = self.palette;
        let height = self.logical_height();
        let card = R::new(12.0, 12.0, WIDTH - 24.0, height - 24.0);
        let field = self.field_rect();
        let error = self.error.clone();
        let prompt = self.prompt.clone();
        let kit = &mut self.kit;
        self.surface.draw(|painter| {
            kit.begin_frame();
            painter.clear(palette.bg);
            painter.fill(card, 14.0, palette.surface);
            painter.stroke(card, 14.0, 1.0, palette.border);
            painter.text(
                R::new(34.0, 32.0, WIDTH - 68.0, 24.0),
                &prompt,
                TextStyle::new(13.0, true, palette.text),
            );
            painter.text(
                R::new(34.0, 58.0, WIDTH - 68.0, 18.0),
                "설정에서 언제든 다시 변경할 수 있습니다.",
                TextStyle::new(10.0, false, palette.muted),
            );
            painter.fill(field, 10.0, palette.surface_alt);
            let focused = kit.focus == Some(W_FIELD);
            let border = if error.is_some() {
                palette.danger
            } else if focused {
                palette.accent
            } else {
                palette.border
            };
            painter.stroke(field, 10.0, if focused { 2.0 } else { 1.0 }, border);
            kit.focusables.push(W_FIELD);
            kit.hit(field, W_FIELD);
            let mut y = field.bottom() + 18.0;
            if let Some(message) = &error {
                painter.text(
                    R::new(34.0, field.bottom() + 6.0, WIDTH - 68.0, 18.0),
                    message,
                    TextStyle::new(10.0, false, palette.danger),
                );
                y += ERROR_EXTRA;
            }
            let confirm = R::new(WIDTH - 34.0 - 88.0, y, 88.0, 36.0);
            let cancel = R::new(confirm.x - 6.0 - 88.0, y, 88.0, 36.0);
            let cancel_style = ButtonStyle { fill: None, ..ButtonStyle::outline(palette) };
            button(
                painter,
                kit,
                palette,
                W_CANCEL,
                cancel,
                "취소",
                ButtonStyle { text: palette.muted, ..cancel_style },
                true,
            );
            button(painter, kit, palette, W_CONFIRM, confirm, "확인", ButtonStyle::primary(palette), true);
        });
        self.place_edit();
    }

    fn place_edit(&mut self) {
        if self.edit_placed {
            return;
        }
        self.edit_placed = true;
        let scale = self.scale();
        let font = EditFont::create((12.0 * scale).round() as i32);
        font.apply(self.edit);
        self._font = Some(font);
        let field = self.field_rect();
        let inner = R::new(field.x + 12.0, field.y + (field.h - 20.0) / 2.0, field.w - 24.0, 20.0);
        let rect = physical(inner, scale);
        // SAFETY: positioning a child window owned by this thread.
        unsafe {
            let _ = SetWindowPos(
                self.edit,
                None,
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
    }

    fn confirm(&mut self) {
        let value = strip(&edit_text(self.edit)).to_string();
        if let Some(message) = (self.validator)(&value) {
            let grew = self.error.is_none();
            self.error = Some(message);
            if grew {
                self.grow_for_error();
            }
            self.focus(W_FIELD);
            self.invalidate();
            return;
        }
        self.result = Some(value);
        self.done = true;
    }

    fn grow_for_error(&mut self) {
        let mut rect = RECT::default();
        // SAFETY: `rect` is a valid out-param; the window belongs to this thread.
        unsafe {
            let _ = GetWindowRect(self.hwnd, &mut rect);
            let _ = SetWindowPos(
                self.hwnd,
                None,
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top + (ERROR_EXTRA * self.scale()) as i32,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
        self.surface.sync_size();
    }

    fn cancel(&mut self) {
        self.result = None;
        self.done = true;
    }

    fn focus(&mut self, id: Wid) {
        self.kit.focus = Some(id);
        let target = if id == W_FIELD { self.edit } else { self.hwnd };
        // SAFETY: focusing windows owned by this thread.
        unsafe {
            let _ = SetFocus(Some(target));
        }
        self.invalidate();
    }

    fn invoke(&mut self, id: Wid) {
        match id {
            W_CONFIRM => self.confirm(),
            W_CANCEL => self.cancel(),
            W_FIELD => self.focus(W_FIELD),
            _ => {}
        }
    }

    fn key_down(&mut self, vk: u16) {
        // SAFETY: GetKeyState only reads this thread's input state.
        let shift = unsafe { GetKeyState(VK_SHIFT.0.into()) < 0 };
        if vk == VK_ESCAPE.0 {
            self.cancel();
        } else if vk == VK_TAB.0 {
            if let Some(next) = self.kit.next_focus(shift) {
                self.focus(next);
            }
        } else if vk == VK_RETURN.0 {
            match self.kit.focus {
                Some(W_CANCEL) => self.cancel(),
                _ => self.confirm(),
            }
        } else if vk == VK_SPACE.0 {
            if let Some(id) = self.kit.focus {
                self.invoke(id);
            }
        }
    }
}

// The window user-data slot holds a type-erased pointer to the RefCell that
// lives on `ask_text`'s stack for the entire nested message loop.
type SharedState<'a> = RefCell<DialogState<'a>>;

fn state_of(hwnd: HWND) -> Option<&'static SharedState<'static>> {
    // SAFETY: the slot is either zero or points at the state owned by
    // `ask_text`, which clears it before the state is dropped.
    unsafe {
        let pointer = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const SharedState<'static>;
        pointer.as_ref()
    }
}

fn with_state(hwnd: HWND, body: impl FnOnce(&mut DialogState<'static>)) {
    if let Some(cell) = state_of(hwnd) {
        if let Ok(mut state) = cell.try_borrow_mut() {
            body(&mut state);
        }
    }
}

unsafe extern "system" fn dialog_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    guard_panic(LRESULT(0), || match message {
        WM_CTLCOLOREDIT => edit_colors(wparam),
        WM_ERASEBKGND => LRESULT(1),
        WM_PAINT => run_paint(hwnd, || with_state(hwnd, DialogState::paint)),
        WM_MOUSEMOVE => {
            with_state(hwnd, |state| {
                let (x, y) = pointer(lparam, state.scale());
                if state.kit.mouse_move(hwnd, x, y) {
                    state.invalidate();
                }
            });
            LRESULT(0)
        }
        WM_MOUSELEAVE => {
            with_state(hwnd, |state| {
                state.kit.mouse_leave();
                state.invalidate();
            });
            LRESULT(0)
        }
        WM_LBUTTONDOWN => {
            with_state(hwnd, |state| {
                let (x, y) = pointer(lparam, state.scale());
                state.kit.mouse_down(x, y);
            });
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            with_state(hwnd, |state| {
                let (x, y) = pointer(lparam, state.scale());
                if let Some(id) = state.kit.mouse_up(x, y) {
                    state.invoke(id);
                }
            });
            LRESULT(0)
        }
        WM_KEYDOWN => {
            with_state(hwnd, |state| state.key_down(wparam.0 as u16));
            LRESULT(0)
        }
        WM_CLOSE => {
            with_state(hwnd, DialogState::cancel);
            LRESULT(0)
        }
        // SAFETY: forwarding unmodified parameters for this window.
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    })
}

unsafe extern "system" fn dialog_edit_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    guard_panic(LRESULT(0), || {
        // SAFETY: forwarding to the next subclass proc; the parent lookup only
        // reads this window's own relationship.
        let forward = || unsafe { DefSubclassProc(hwnd, message, wparam, lparam) };
        let parent = unsafe { GetParent(hwnd) }.unwrap_or_default();
        match message {
            WM_KEYDOWN => {
                let vk = wparam.0 as u16;
                if vk == VK_RETURN.0 || vk == VK_ESCAPE.0 || vk == VK_TAB.0 {
                    with_state(parent, |state| state.key_down(vk));
                    LRESULT(0)
                } else {
                    forward()
                }
            }
            WM_CHAR if matches!(wparam.0, 0x0D | 0x1B | 0x09) => LRESULT(0),
            _ => forward(),
        }
    })
}

fn register_class(instance: HINSTANCE) {
    let class = WNDCLASSW {
        lpfnWndProc: Some(dialog_proc),
        hInstance: instance,
        lpszClassName: w!("QuickAccessDialog"),
        // SAFETY: loading a stock system cursor.
        hCursor: unsafe { LoadCursorW(None, IDC_ARROW) }.unwrap_or_default(),
        ..Default::default()
    };
    // SAFETY: the class struct refers to static strings and a valid procedure.
    unsafe { RegisterClassW(&class) };
}

fn target_monitor(owner: Option<HWND>) -> (MonitorContext, Option<Point>) {
    if let Some(owner) = owner {
        let mut rect = RECT::default();
        // SAFETY: `rect` is a valid out-param for GetWindowRect.
        if unsafe { GetWindowRect(owner, &mut rect) }.is_ok() {
            let center = Point { x: (rect.left + rect.right) / 2, y: (rect.top + rect.bottom) / 2 };
            if let Some(monitor) = monitor_at(center) {
                return (monitor, Some(center));
            }
        }
    }
    (monitor_at_cursor(), None)
}

/// Show the dialog modally; `None` when cancelled.
pub fn ask_text(
    owner: Option<HWND>,
    palette: &'static Palette,
    title: &str,
    prompt: &str,
    initial_value: &str,
    validator: Validator<'_>,
) -> Option<String> {
    // SAFETY: GetModuleHandleW(None) returns this executable's handle.
    let instance: HINSTANCE = unsafe { GetModuleHandleW(None) }.ok()?.into();
    register_class(instance);
    // SAFETY: querying visibility of a window this process owns.
    let owner = owner.filter(|window| unsafe { IsWindowVisible(*window) }.as_bool());
    let (monitor, parent_center) = target_monitor(owner);
    let title_units = wide(title);
    let style = WS_CAPTION | WS_SYSMENU;
    // SAFETY: standard window creation on the UI thread; the title outlives it.
    let (hwnd, edit) = unsafe {
        let hwnd = CreateWindowExW(
            WS_EX_DLGMODALFRAME,
            w!("QuickAccessDialog"),
            PCWSTR(title_units.as_ptr()),
            style,
            monitor.work_area.left + 8,
            monitor.work_area.top + 8,
            400,
            300,
            owner,
            None,
            Some(instance),
            None,
        )
        .ok()?;
        let edit = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("EDIT"),
            PCWSTR::null(),
            WS_CHILD | WS_VISIBLE | WINDOW_STYLE(ES_AUTOHSCROLL as u32),
            0,
            0,
            100,
            20,
            Some(hwnd),
            Some(HMENU(1 as *mut _)),
            Some(instance),
            None,
        )
        .ok()?;
        let _ = SetWindowSubclass(edit, Some(dialog_edit_proc), 1, 0);
        (hwnd, edit)
    };
    apply_window_chrome(hwnd, palette, false);
    set_edit_palette(palette);
    set_edit_text(edit, initial_value);
    select_all(edit);

    // SAFETY: querying a window created just above.
    let dpi = unsafe { GetDpiForWindow(hwnd) }.max(96);
    let scale = dpi as f32 / 96.0;
    let mut frame = RECT {
        left: 0,
        top: 0,
        right: (WIDTH * scale).round() as i32,
        bottom: (HEIGHT * scale).round() as i32,
    };
    // SAFETY: `frame` is a valid in/out rectangle.
    unsafe {
        let _ = AdjustWindowRectExForDpi(&mut frame, style, false, WS_EX_DLGMODALFRAME, dpi);
    }
    let size = Size { width: frame.right - frame.left, height: frame.bottom - frame.top };
    let position = match parent_center {
        Some(center) => clamp_to_work_area(
            Point { x: center.x - size.width / 2, y: center.y - size.height / 2 },
            size,
            monitor.work_area,
        ),
        None => center_in_work_area(size, monitor.work_area),
    };

    let cell: RefCell<DialogState<'_>> = RefCell::new(DialogState {
        hwnd,
        edit,
        surface: Surface::new(hwnd, dpi),
        kit: Kit::default(),
        palette,
        prompt: prompt.to_string(),
        validator,
        error: None,
        result: None,
        done: false,
        dpi,
        _font: None,
        edit_placed: false,
    });
    cell.borrow_mut().kit.focus = Some(W_FIELD);

    // SAFETY: the pointer stays valid until cleared below, before `cell` drops;
    // the owner is re-enabled before its modal child goes away.
    unsafe {
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, &cell as *const _ as isize);
        let z_order = if owner.is_some() { HWND_TOP } else { HWND_TOPMOST };
        let _ = SetWindowPos(hwnd, Some(z_order), position.x, position.y, size.width, size.height, SWP_NOACTIVATE);
        if let Some(owner) = owner {
            let _ = EnableWindow(owner, false);
        }
        let _ = ShowWindow(hwnd, SW_SHOW);
        let _ = SetForegroundWindow(hwnd);
        let _ = SetFocus(Some(edit));

        let mut message = MSG::default();
        while !cell.borrow().done {
            let status = GetMessageW(&mut message, None, 0, 0);
            if status.0 <= 0 {
                // Let the outer loop see the quit request too.
                PostQuitMessage(0);
                break;
            }
            let _ = TranslateMessage(&message);
            DispatchMessageW(&message);
        }

        if let Some(owner) = owner {
            let _ = EnableWindow(owner, true);
            let _ = SetForegroundWindow(owner);
        }
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        let _ = DestroyWindow(hwnd);
    }
    cell.into_inner().result
}

pub fn ask_display_name(owner: Option<HWND>, palette: &'static Palette, initial: &str, title: &str) -> Option<String> {
    ask_text(owner, palette, title, "런처에 표시할 이름을 입력하세요.", initial, &|value: &str| {
        value.is_empty().then(|| "표시명을 입력해 주세요.".to_string())
    })
}
