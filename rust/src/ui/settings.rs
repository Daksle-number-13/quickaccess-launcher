//! Launcher item and application settings window.

use std::collections::HashMap;

use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Controls::WM_MOUSELEAVE;
use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
use windows::Win32::UI::HiDpi::{AdjustWindowRectExForDpi, GetDpiForWindow};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetKeyState, ReleaseCapture, SetCapture, SetFocus, VK_CONTROL, VK_ESCAPE, VK_RETURN, VK_SHIFT, VK_SPACE,
    VK_TAB,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, GetClientRect, LoadCursorW, RegisterClassW,
    SetForegroundWindow, SetWindowPos, ShowWindow, CS_HREDRAW, CS_VREDRAW, ES_AUTOHSCROLL, HMENU,
    HWND_TOP, IDC_ARROW, MINMAXINFO, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER, SW_HIDE,
    SW_SHOW, SW_SHOWNA, WINDOW_EX_STYLE, WINDOW_STYLE, WM_CHAR, WM_CLOSE, WM_COMMAND, WM_CTLCOLOREDIT,
    WM_DPICHANGED, WM_ERASEBKGND, WM_GETMINMAXINFO, WM_KEYDOWN, WM_LBUTTONDOWN, WM_LBUTTONUP,
    WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_PAINT, WM_SIZE, WNDCLASSW, WS_CHILD, WS_EX_APPWINDOW,
    WS_OVERLAPPEDWINDOW,
};

use crate::app::{dispatch, with_app, Action};
use crate::diagnostics::{APP_AUTHOR, APP_VERSION};
use crate::geometry::{center_in_work_area, settings_dimensions, Size};
use crate::hotkey::describe_conflict_risk;
use crate::model::{AppearanceMode, ItemType, LauncherConfig, LauncherItem};
use crate::platform::MonitorContext;
use crate::startup_rules::{RegistrationState, RegistrationStatus};
use crate::ui::gfx::{Align, Painter, Surface, TextStyle, R};
use crate::ui::kit::{
    apply_window_chrome, button, edit_colors, edit_text, guard_panic, physical, pointer, run_paint,
    scroll_from_pointer, scrollbar, segmented, select_all, set_edit_palette, set_edit_text, switch,
    wheel_delta, ButtonStyle, EditFont, Kit, Wid,
};
use crate::ui::theme::{Color, Palette, GLYPH_DELETE, GLYPH_FILE, GLYPH_FOLDER, GLYPH_LINK};

const SIDEBAR_WIDTH: f32 = 214.0;
const EN_CHANGE: u32 = 0x0300;
const WHEEL_STEP: f32 = 48.0;

const W_NAV: Wid = 10;
const W_ADD_FOLDER: Wid = 20;
const W_ADD_FILE: Wid = 21;
const W_ADD_LINK: Wid = 22;
const W_APPEARANCE: Wid = 30;
const W_COLUMNS: Wid = 40;
const W_STARTUP: Wid = 50;
const W_APPLY_HOTKEYS: Wid = 51;
const W_EDIT_PANEL: Wid = 52;
const W_EDIT_QUICK: Wid = 53;
const W_UPDATES: Wid = 54;
const W_IMPORT: Wid = 55;
const W_EXPORT: Wid = 56;
const W_DIAGNOSTICS: Wid = 57;
const W_EXIT: Wid = 58;
const W_SCROLL: Wid = 59;
const W_ROW: Wid = 1000;
const ROW_ACTIONS: Wid = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Page {
    Items,
    Shortcuts,
    Appearance,
    About,
}

const PAGES: [Page; 4] = [Page::Items, Page::Shortcuts, Page::Appearance, Page::About];

pub fn registration_text(status: Option<&RegistrationStatus>) -> (&'static str, Tone) {
    match status.map(|status| status.state) {
        Some(RegistrationState::Correct) => ("실제 상태 · 정상 등록", Tone::Accent),
        Some(RegistrationState::Absent) => ("실제 상태 · 미등록", Tone::Muted),
        Some(RegistrationState::Stale) => ("실제 상태 · 이전 경로", Tone::Warning),
        Some(RegistrationState::Unreadable) => ("실제 상태 · 확인 불가", Tone::Warning),
        None => ("실제 상태 · 확인 전", Tone::Muted),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Muted,
    Accent,
    Warning,
}

fn tone_color(tone: Tone, palette: &Palette) -> Color {
    match tone {
        Tone::Muted => palette.muted,
        Tone::Accent => palette.accent,
        Tone::Warning => palette.warning,
    }
}

pub fn format_hotkey(value: &str) -> String {
    value
        .split('+')
        .filter(|part| !part.is_empty())
        .map(|part| {
            let mut chars = part.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars.flat_map(char::to_lowercase)).collect(),
                None => String::new(),
            }
        })
        .collect::<Vec<String>>()
        .join("  +  ")
}

pub struct Settings {
    pub hwnd: HWND,
    edits: [HWND; 2],
    surface: Surface,
    kit: Kit,
    pub shown: bool,
    dpi: u32,
    page: Page,
    scroll: HashMap<Page, f32>,
    content_height: f32,
    viewport: R,
    dragging_scroll: bool,
    hotkeys_dirty: bool,
    startup_status: Option<RegistrationStatus>,
    pub transfer_note: Option<(String, Tone)>,
    pub diagnostics_note: Option<(String, Tone)>,
    edit_font: Option<EditFont>,
    edit_font_pixels: i32,
    ellipsis_cache: HashMap<(String, u32), String>,
    last_edit_rects: [Option<R>; 2],
    frame_items: Vec<(String, i64, ItemType)>,
}

fn register_class(instance: HINSTANCE) {
    let class = WNDCLASSW {
        style: CS_HREDRAW | CS_VREDRAW,
        lpfnWndProc: Some(settings_proc),
        hInstance: instance,
        lpszClassName: w!("QuickAccessSettings"),
        // SAFETY: loading a stock system cursor.
        hCursor: unsafe { LoadCursorW(None, IDC_ARROW) }.unwrap_or_default(),
        hIcon: crate::platform::tray::app_icon(32, 32).unwrap_or_default(),
        ..Default::default()
    };
    // SAFETY: the class struct refers to static strings and a valid procedure.
    unsafe { RegisterClassW(&class) };
}

impl Settings {
    pub fn create() -> windows::core::Result<Self> {
        // SAFETY: standard window creation on the UI thread; handles live for
        // the process lifetime.
        unsafe {
            let instance: HINSTANCE = GetModuleHandleW(None)?.into();
            register_class(instance);
            let hwnd = CreateWindowExW(
                WS_EX_APPWINDOW,
                w!("QuickAccessSettings"),
                w!("QuickAccess"),
                WS_OVERLAPPEDWINDOW,
                100,
                100,
                1000,
                700,
                None,
                None,
                Some(instance),
                None,
            )?;
            let mut edits = [HWND::default(); 2];
            for (index, slot) in edits.iter_mut().enumerate() {
                let edit = CreateWindowExW(
                    WINDOW_EX_STYLE(0),
                    w!("EDIT"),
                    PCWSTR::null(),
                    WS_CHILD | WINDOW_STYLE(ES_AUTOHSCROLL as u32),
                    0,
                    0,
                    100,
                    20,
                    Some(hwnd),
                    Some(HMENU((index + 1) as *mut _)),
                    Some(instance),
                    None,
                )?;
                let _ = SetWindowSubclass(edit, Some(hotkey_edit_proc), 1, 0);
                *slot = edit;
            }
            let dpi = GetDpiForWindow(hwnd).max(96);
            Ok(Self {
                hwnd,
                edits,
                surface: Surface::new(hwnd, dpi),
                kit: Kit::default(),
                shown: false,
                dpi,
                page: Page::Items,
                scroll: HashMap::new(),
                content_height: 0.0,
                viewport: R::default(),
                dragging_scroll: false,
                hotkeys_dirty: false,
                startup_status: None,
                transfer_note: None,
                diagnostics_note: None,
                edit_font: None,
                edit_font_pixels: 0,
                ellipsis_cache: HashMap::new(),
                last_edit_rects: [None; 2],
                frame_items: Vec::new(),
            })
        }
    }

    fn scale(&self) -> f32 {
        self.dpi as f32 / 96.0
    }

    pub fn invalidate(&self) {
        // SAFETY: invalidating a window owned by this thread.
        unsafe {
            let _ = InvalidateRect(Some(self.hwnd), None, false);
        }
    }

    pub fn set_startup_status(&mut self, status: Option<RegistrationStatus>) {
        self.startup_status = status;
    }

    /// Load current values. Unapplied hotkey edits are preserved.
    pub fn refresh(&mut self, config: &LauncherConfig) {
        if !self.hotkeys_dirty {
            set_edit_text(self.edits[0], &config.hotkey);
            set_edit_text(self.edits[1], &config.quick_add_hotkey);
        }
        self.invalidate();
    }

    pub fn apply_palette(&self, palette: &Palette) {
        apply_window_chrome(self.hwnd, palette, false);
        set_edit_palette(palette);
    }

    /// Size and center the window on the cursor's monitor, then reveal it.
    pub fn show(&mut self, config: &LauncherConfig, palette: &Palette, monitor: &MonitorContext) {
        self.apply_palette(palette);
        if self.shown {
            // A second click only raises the window; it must not chase a
            // cursor that moved to another monitor.
            self.refresh(config);
            // SAFETY: raising a window owned by this thread.
            unsafe {
                let _ = ShowWindow(self.hwnd, SW_SHOW);
                let _ = SetForegroundWindow(self.hwnd);
            }
            return;
        }
        let work = monitor.work_area;
        // SAFETY: moving a hidden window onto the target monitor first means
        // the first visible frame is rendered at the destination DPI.
        unsafe {
            let _ = SetWindowPos(self.hwnd, None, work.left + 8, work.top + 8, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
            self.dpi = GetDpiForWindow(self.hwnd).max(96);
        }
        let scale = f64::from(self.dpi) / 96.0;
        let (logical_width, logical_height) = settings_dimensions(work.width(), work.height(), scale);
        let mut frame = RECT {
            left: 0,
            top: 0,
            right: (f64::from(logical_width) * scale).round() as i32,
            bottom: (f64::from(logical_height) * scale).round() as i32,
        };
        // SAFETY: `frame` is a valid in/out rectangle.
        unsafe {
            let _ = AdjustWindowRectExForDpi(&mut frame, WS_OVERLAPPEDWINDOW, false, WS_EX_APPWINDOW, self.dpi);
        }
        let size = Size { width: frame.right - frame.left, height: frame.bottom - frame.top };
        let position = center_in_work_area(size, work);
        // SAFETY: as above.
        unsafe {
            let _ = SetWindowPos(self.hwnd, Some(HWND_TOP), position.x, position.y, size.width, size.height, SWP_NOACTIVATE);
        }
        self.surface.set_dpi(self.dpi);
        self.surface.sync_size();
        self.refresh(config);
        self.shown = true;
        // SAFETY: revealing and raising a window owned by this thread.
        unsafe {
            let _ = ShowWindow(self.hwnd, SW_SHOW);
            let _ = SetForegroundWindow(self.hwnd);
            let _ = SetFocus(Some(self.hwnd));
        }
    }

    pub fn hide(&mut self) {
        self.shown = false;
        // SAFETY: hiding a window owned by this thread.
        unsafe {
            let _ = ShowWindow(self.hwnd, SW_HIDE);
        }
    }

    pub fn is_visible(&self) -> bool {
        self.shown
    }

    pub fn select_page(&mut self, page: Page) {
        self.page = page;
        self.scroll.insert(page, 0.0);
        self.kit.focus = None;
        // SAFETY: focusing this thread's own window.
        unsafe {
            let _ = SetFocus(Some(self.hwnd));
        }
        self.invalidate();
    }

    pub fn on_dpi_changed(&mut self, dpi: u32, suggested: &RECT) {
        self.dpi = dpi.max(96);
        // SAFETY: applying the rectangle Windows supplied for the new DPI.
        unsafe {
            let _ = SetWindowPos(
                self.hwnd,
                None,
                suggested.left,
                suggested.top,
                suggested.right - suggested.left,
                suggested.bottom - suggested.top,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
        self.surface.set_dpi(self.dpi);
        self.surface.sync_size();
        self.last_edit_rects = [None; 2];
        self.invalidate();
    }

    pub fn on_resized(&mut self) {
        self.surface.sync_size();
        self.invalidate();
    }

    pub fn hotkey_edited(&mut self) {
        self.hotkeys_dirty = true;
        self.invalidate();
    }

    pub fn hotkeys_applied(&mut self) {
        self.hotkeys_dirty = false;
    }

    pub fn hotkey_values(&self) -> (String, String) {
        (
            crate::pyish::strip(&edit_text(self.edits[0])).to_string(),
            crate::pyish::strip(&edit_text(self.edits[1])).to_string(),
        )
    }

    pub fn paint(&mut self, config: &LauncherConfig, palette: &Palette) {
        let mut client = RECT::default();
        // SAFETY: `client` is a valid out-param for a window on this thread.
        unsafe {
            let _ = GetClientRect(self.hwnd, &mut client);
        }
        let scale = self.scale();
        let width = (client.right - client.left) as f32 / scale;
        let height = (client.bottom - client.top) as f32 / scale;
        self.frame_items = config
            .items
            .iter()
            .map(|item| (item.id.clone(), item.order, item.item_type))
            .collect();

        let warnings = {
            let mut warnings: Vec<&str> = Vec::new();
            for edit in self.edits {
                if let Some(risk) = describe_conflict_risk(crate::pyish::strip(&edit_text(edit))) {
                    if !warnings.contains(&risk) {
                        warnings.push(risk);
                    }
                }
            }
            warnings
        };

        let mut frame = Frame {
            config,
            palette,
            width,
            height,
            compact: width < 800.0,
            ultra: width < 520.0,
            page: self.page,
            scroll: self.scroll.get(&self.page).copied().unwrap_or(0.0),
            warnings,
            startup: self.startup_status.as_ref(),
            transfer_note: self.transfer_note.as_ref(),
            diagnostics_note: self.diagnostics_note.as_ref(),
            ellipsis: &mut self.ellipsis_cache,
            edit_rects: [None; 2],
            content_height: 0.0,
            viewport: R::default(),
        };
        let kit = &mut self.kit;
        self.surface.draw(|painter| {
            kit.begin_frame();
            painter.clear(palette.bg);
            frame.paint(painter, kit);
        });
        let edit_rects = frame.edit_rects;
        self.content_height = frame.content_height;
        self.viewport = frame.viewport;
        let max = (self.content_height - self.viewport.h).max(0.0);
        let current = self.scroll.get(&self.page).copied().unwrap_or(0.0);
        if current > max {
            self.scroll.insert(self.page, max);
            self.invalidate();
        }
        self.place_edits(edit_rects);
    }

    fn place_edits(&mut self, rects: [Option<R>; 2]) {
        let scale = self.scale();
        let pixels = (12.0 * scale).round() as i32;
        if self.edit_font.is_none() || self.edit_font_pixels != pixels {
            let font = EditFont::create(pixels);
            for edit in self.edits {
                font.apply(edit);
            }
            self.edit_font = Some(font);
            self.edit_font_pixels = pixels;
        }
        for (index, rect) in rects.iter().enumerate() {
            if self.last_edit_rects[index] == *rect {
                continue;
            }
            self.last_edit_rects[index] = *rect;
            let edit = self.edits[index];
            match rect {
                Some(field) => {
                    let inner = R::new(field.x + 10.0, field.y + (field.h - 18.0) / 2.0, field.w - 20.0, 18.0);
                    let physical_rect = physical(inner, scale);
                    // SAFETY: positioning a child window owned by this thread.
                    unsafe {
                        let _ = SetWindowPos(
                            edit,
                            None,
                            physical_rect.left,
                            physical_rect.top,
                            physical_rect.right - physical_rect.left,
                            physical_rect.bottom - physical_rect.top,
                            SWP_NOZORDER | SWP_NOACTIVATE,
                        );
                        let _ = ShowWindow(edit, SW_SHOWNA);
                    }
                }
                // SAFETY: hiding a child window owned by this thread.
                None => unsafe {
                    let _ = ShowWindow(edit, SW_HIDE);
                },
            }
        }
    }

    pub fn mouse_move(&mut self, lparam: LPARAM) {
        let (x, y) = pointer(lparam, self.scale());
        if self.dragging_scroll {
            let value = scroll_from_pointer(y, self.scroll_track(), self.content_height);
            self.scroll.insert(self.page, value);
            self.invalidate();
            return;
        }
        if self.kit.mouse_move(self.hwnd, x, y) {
            self.invalidate();
        }
    }

    pub fn mouse_leave(&mut self) {
        self.kit.mouse_leave();
        self.invalidate();
    }

    fn scroll_track(&self) -> R {
        R::new(self.viewport.right() + 2.0, self.viewport.y, 10.0, self.viewport.h)
    }

    pub fn mouse_down(&mut self, lparam: LPARAM) {
        let (x, y) = pointer(lparam, self.scale());
        let hit = self.kit.mouse_down(x, y);
        if hit == Some(W_SCROLL) {
            self.dragging_scroll = true;
            let value = scroll_from_pointer(y, self.scroll_track(), self.content_height);
            self.scroll.insert(self.page, value);
            // SAFETY: capturing the mouse for a window owned by this thread.
            unsafe {
                SetCapture(self.hwnd);
            }
        } else if hit.is_none() {
            self.kit.focus = None;
            // SAFETY: focusing this thread's own window.
            unsafe {
                let _ = SetFocus(Some(self.hwnd));
            }
        }
        self.invalidate();
    }

    pub fn mouse_up(&mut self, lparam: LPARAM, config: &LauncherConfig) -> Vec<Action> {
        if self.dragging_scroll {
            self.dragging_scroll = false;
            self.kit.pressed = None;
            // SAFETY: releasing this thread's mouse capture.
            unsafe {
                let _ = ReleaseCapture();
            }
            return Vec::new();
        }
        let (x, y) = pointer(lparam, self.scale());
        match self.kit.mouse_up(x, y) {
            Some(id) => self.invoke(id, config),
            None => Vec::new(),
        }
    }

    pub fn wheel(&mut self, delta: i32) {
        let notches = delta as f32 / 120.0;
        let max = (self.content_height - self.viewport.h).max(0.0);
        let current = self.scroll.get(&self.page).copied().unwrap_or(0.0);
        self.scroll.insert(self.page, (current - notches * WHEEL_STEP).clamp(0.0, max));
        self.invalidate();
    }

    fn invoke(&mut self, id: Wid, config: &LauncherConfig) -> Vec<Action> {
        if (W_NAV..W_NAV + 4).contains(&id) {
            self.select_page(PAGES[(id - W_NAV) as usize]);
            return Vec::new();
        }
        if (W_APPEARANCE..W_APPEARANCE + 3).contains(&id) {
            let mode = [AppearanceMode::System, AppearanceMode::Light, AppearanceMode::Dark]
                [(id - W_APPEARANCE) as usize];
            return vec![Action::SetAppearance(mode)];
        }
        if (W_COLUMNS..W_COLUMNS + 4).contains(&id) {
            return vec![Action::SetColumns(i64::from(id - W_COLUMNS) + 2)];
        }
        if id >= W_ROW {
            let index = ((id - W_ROW) / ROW_ACTIONS) as usize;
            let Some(item) = config.items.get(index) else {
                return Vec::new();
            };
            return vec![match (id - W_ROW) % ROW_ACTIONS {
                0 => Action::EditItem(item.id.clone()),
                1 => Action::DeleteItem(item.id.clone()),
                2 => Action::MoveItem(item.id.clone(), item.order - 1),
                _ => Action::MoveItem(item.id.clone(), item.order + 1),
            }];
        }
        match id {
            W_ADD_FOLDER => vec![Action::AddFolder],
            W_ADD_FILE => vec![Action::AddFile],
            W_ADD_LINK => vec![Action::AddLink],
            W_STARTUP => vec![Action::SetStartup(!config.run_on_startup)],
            W_UPDATES => vec![Action::SetUpdates(!config.check_updates)],
            W_APPLY_HOTKEYS => {
                let (panel, quick) = self.hotkey_values();
                vec![Action::ApplyHotkeys(panel, quick)]
            }
            W_IMPORT => vec![Action::ImportConfig],
            W_EXPORT => vec![Action::ExportConfig],
            W_DIAGNOSTICS => vec![Action::CopyDiagnostics],
            W_EXIT => vec![Action::RequestShutdown],
            W_EDIT_PANEL | W_EDIT_QUICK => {
                self.focus_widget(id);
                Vec::new()
            }
            _ => Vec::new(),
        }
    }

    fn focus_widget(&mut self, id: Wid) {
        self.kit.focus = Some(id);
        let target = match id {
            W_EDIT_PANEL => self.edits[0],
            W_EDIT_QUICK => self.edits[1],
            _ => self.hwnd,
        };
        // SAFETY: focusing windows owned by this thread.
        unsafe {
            let _ = SetFocus(Some(target));
        }
        if target != self.hwnd {
            select_all(target);
        }
        if id >= W_ROW {
            self.scroll_row_into_view(((id - W_ROW) / ROW_ACTIONS) as usize);
        }
        self.invalidate();
    }

    fn scroll_row_into_view(&mut self, row: usize) {
        let pitch = row_height(self.ultra_now());
        let top = row as f32 * pitch;
        let current = self.scroll.get(&self.page).copied().unwrap_or(0.0);
        let next = if top < current {
            top
        } else if top + pitch > current + self.viewport.h {
            top + pitch - self.viewport.h
        } else {
            current
        };
        self.scroll.insert(self.page, next.max(0.0));
    }

    fn ultra_now(&self) -> bool {
        let mut client = RECT::default();
        // SAFETY: `client` is a valid out-param for a window on this thread.
        unsafe {
            let _ = GetClientRect(self.hwnd, &mut client);
        }
        ((client.right - client.left) as f32 / self.scale()) < 520.0
    }

    pub fn tab(&mut self, reverse: bool) {
        if let Some(next) = self.kit.next_focus(reverse) {
            self.focus_widget(next);
        }
    }

    pub fn key_down(&mut self, vk: u16, config: &LauncherConfig) -> Vec<Action> {
        // SAFETY: GetKeyState only reads this thread's input state.
        let (control, shift) = unsafe {
            (GetKeyState(VK_CONTROL.0.into()) < 0, GetKeyState(VK_SHIFT.0.into()) < 0)
        };
        if vk == VK_ESCAPE.0 {
            self.hide();
        } else if vk == VK_TAB.0 {
            self.tab(shift);
        } else if control && (0x31..=0x34).contains(&vk) {
            self.select_page(PAGES[(vk - 0x31) as usize]);
        } else if vk == VK_RETURN.0 || vk == VK_SPACE.0 {
            if let Some(id) = self.kit.focus {
                return self.invoke(id, config);
            }
        }
        Vec::new()
    }
}

fn row_height(ultra: bool) -> f32 {
    if ultra {
        106.0
    } else {
        70.0
    }
}

struct Frame<'a> {
    config: &'a LauncherConfig,
    palette: &'a Palette,
    width: f32,
    height: f32,
    compact: bool,
    ultra: bool,
    page: Page,
    scroll: f32,
    warnings: Vec<&'a str>,
    startup: Option<&'a RegistrationStatus>,
    transfer_note: Option<&'a (String, Tone)>,
    diagnostics_note: Option<&'a (String, Tone)>,
    ellipsis: &'a mut HashMap<(String, u32), String>,
    edit_rects: [Option<R>; 2],
    content_height: f32,
    viewport: R,
}

impl Frame<'_> {
    fn sidebar_width(&self) -> f32 {
        if self.ultra {
            104.0
        } else if self.compact {
            160.0
        } else {
            SIDEBAR_WIDTH
        }
    }

    fn padding(&self) -> f32 {
        if self.ultra {
            8.0
        } else if self.compact {
            16.0
        } else {
            28.0
        }
    }

    fn paint(&mut self, painter: &mut Painter<'_>, kit: &mut Kit) {
        // Page bodies first: header and sidebar widgets registered later win
        // hit-testing over any partially scrolled content.
        let page_left = self.sidebar_width();
        let page_width = (self.width - page_left).max(1.0);
        match self.page {
            Page::Items => self.paint_items_page(painter, kit, page_left, page_width),
            Page::Shortcuts => self.paint_shortcuts_page(painter, kit, page_left, page_width),
            Page::Appearance => self.paint_appearance_page(painter, kit, page_left, page_width),
            Page::About => self.paint_about_page(painter, kit, page_left, page_width),
        }
        self.paint_sidebar(painter, kit);
    }

    fn paint_sidebar(&self, painter: &mut Painter<'_>, kit: &mut Kit) {
        let palette = self.palette;
        let width = self.sidebar_width();
        painter.fill(R::new(0.0, 0.0, width, self.height), 0.0, palette.surface);
        let brand_x = if self.ultra {
            8.0
        } else if self.compact {
            12.0
        } else {
            20.0
        };
        let brand = painter.graphics.brand();
        painter.image("\0brand", &brand, R::new(brand_x, 22.0, 38.0, 38.0));
        if !self.ultra {
            let text_x = brand_x + 38.0 + 11.0;
            painter.text(
                R::new(text_x, 21.0, width - text_x - 4.0, 22.0),
                "QuickAccess",
                TextStyle::new(15.0, true, palette.text),
            );
            painter.text(
                R::new(text_x, 42.0, width - text_x - 4.0, 16.0),
                "Launcher",
                TextStyle::new(10.0, false, palette.muted),
            );
            painter.text(
                R::new(24.0, 86.0, width - 30.0, 16.0),
                "관리",
                TextStyle::new(10.0, true, palette.muted),
            );
        }
        let labels: [&str; 4] = if self.ultra {
            ["항목", "단축키", "화면", "정보"]
        } else if self.compact {
            ["01  항목", "02  단축키", "03  화면", "04  정보"]
        } else {
            ["01   바로가기", "02   단축키·시작", "03   화면·패널", "04   앱 정보"]
        };
        let nav_padding = if self.ultra {
            6.0
        } else if self.compact {
            8.0
        } else {
            14.0
        };
        let start = if self.ultra { 72.0 } else { 108.0 };
        for (index, label) in labels.iter().enumerate() {
            let id = W_NAV + index as Wid;
            let rect = R::new(nav_padding, start + index as f32 * 48.0, width - nav_padding * 2.0, 42.0);
            let active = PAGES[index] == self.page;
            kit.hit(rect, id);
            kit.focusables.push(id);
            let hovered = kit.hover == Some(id);
            if active {
                painter.fill(rect, 10.0, palette.accent_soft);
            } else if hovered {
                painter.fill(rect, 10.0, palette.surface_hover);
            }
            if kit.focus == Some(id) {
                painter.stroke(rect, 10.0, 2.0, palette.accent);
            }
            painter.text(
                rect.inset(12.0, 0.0),
                label,
                TextStyle::new(12.0, true, if active { palette.accent } else { palette.muted }),
            );
        }
        if !self.compact && self.height > 400.0 {
            let hint = R::new(16.0, self.height - 18.0 - 62.0, width - 32.0, 62.0);
            painter.fill(hint, 12.0, palette.surface_alt);
            painter.stroke(hint, 12.0, 1.0, palette.border);
            painter.text(
                R::new(hint.x + 13.0, hint.y + 11.0, hint.w - 26.0, 16.0),
                "빠른 실행",
                TextStyle::new(10.0, true, palette.muted),
            );
            painter.text(
                R::new(hint.x + 13.0, hint.y + 29.0, hint.w - 26.0, 20.0),
                &format_hotkey(&self.config.hotkey),
                TextStyle::new(11.0, true, palette.text),
            );
        }
    }

    /// Title and subtitle; returns the y coordinate where page content starts.
    fn paint_page_title(&self, painter: &Painter<'_>, left: f32, width: f32, title: &str, subtitle: &str) -> f32 {
        let palette = self.palette;
        let padding = self.padding();
        let top = if self.compact { 18.0 } else { 26.0 };
        painter.text(
            R::new(left + padding, top, width - padding * 2.0, 34.0),
            title,
            TextStyle::new(24.0, true, palette.text),
        );
        let bottom_gap = if self.compact { 12.0 } else { 18.0 };
        if self.compact {
            top + 34.0 + bottom_gap
        } else {
            painter.text(
                R::new(left + padding, top + 37.0, width - padding * 2.0, 18.0),
                subtitle,
                TextStyle::new(11.0, false, palette.muted),
            );
            top + 55.0 + bottom_gap
        }
    }

    fn paint_items_page(&mut self, painter: &mut Painter<'_>, kit: &mut Kit, left: f32, width: f32) {
        let palette = self.palette;
        let padding = self.padding();
        let top = if self.compact { 18.0 } else { 26.0 };
        let content_right = left + width - padding;

        painter.text(
            R::new(left + padding, top, 220.0_f32.min(width - padding * 2.0), 34.0),
            "바로가기",
            TextStyle::new(24.0, true, palette.text),
        );
        let list_top;
        if self.compact {
            let gap = 6.0;
            let available = width - padding * 2.0 - gap * 2.0;
            let button_width = available / 3.0;
            let y = top + 34.0 + 8.0;
            let labels = if self.ultra { ["폴더", "파일", "링크"] } else { ["＋  폴더", "＋  파일", "＋  링크"] };
            let styles = [
                ButtonStyle::primary(palette),
                ButtonStyle::soft(palette),
                ButtonStyle::soft(palette),
            ];
            for index in 0..3 {
                let rect = R::new(left + padding + index as f32 * (button_width + gap), y, button_width, 34.0);
                button(painter, kit, palette, W_ADD_FOLDER + index as Wid, rect, labels[index], styles[index], true);
            }
            list_top = y + 34.0 + 12.0;
        } else {
            painter.text(
                R::new(left + padding, top + 37.0, width - padding * 2.0 - 360.0, 18.0),
                "자주 쓰는 파일, 폴더, 웹 링크를 원하는 순서로 관리하세요.",
                TextStyle::new(11.0, false, palette.muted),
            );
            let y = top + 9.0;
            let mut x = content_right - 112.0;
            button(painter, kit, palette, W_ADD_LINK, R::new(x, y, 112.0, 38.0), "＋  웹 링크", ButtonStyle::soft(palette).radius(10.0), true);
            x -= 120.0;
            button(painter, kit, palette, W_ADD_FILE, R::new(x, y, 112.0, 38.0), "＋  파일 추가", ButtonStyle::soft(palette).radius(10.0), true);
            x -= 120.0;
            button(painter, kit, palette, W_ADD_FOLDER, R::new(x, y, 112.0, 38.0), "＋  폴더 추가", ButtonStyle::primary(palette).radius(10.0), true);
            list_top = top + 55.0 + 18.0;
        }

        let card = R::new(left + padding, list_top, (width - padding * 2.0).max(1.0), (self.height - list_top - padding).max(40.0));
        painter.fill(card, 14.0, palette.surface);
        painter.stroke(card, 14.0, 1.0, palette.border);
        painter.text(
            R::new(card.x + 18.0, card.y + 15.0, 160.0, 24.0),
            "등록 항목",
            TextStyle::new(12.0, true, palette.text),
        );
        let badge = R::new(card.right() - 18.0 - 46.0, card.y + 15.0, 46.0, 24.0);
        painter.fill(badge, 8.0, palette.surface_alt);
        painter.text(
            badge,
            &format!("{}개", self.config.items.len()),
            TextStyle::new(10.0, true, palette.muted).centered(),
        );

        let viewport = R::new(card.x + 12.0, card.y + 48.0, (card.w - 30.0).max(1.0), (card.h - 60.0).max(1.0));
        self.viewport = viewport;
        let pitch = row_height(self.ultra);
        let count = self.config.items.len();
        self.content_height = count as f32 * pitch;
        if count == 0 {
            self.paint_empty_list(painter, viewport);
            return;
        }
        let scroll = self.scroll;
        for index in 0..count {
            let row_y = viewport.y + index as f32 * pitch - scroll;
            let rect = R::new(viewport.x, row_y + 4.0, viewport.w, pitch - 8.0);
            let visible = rect.overlaps(viewport);
            self.paint_item_row(painter, kit, index, rect, viewport, visible);
        }
        if self.content_height > viewport.h {
            let track = R::new(viewport.right() + 2.0, viewport.y, 10.0, viewport.h);
            scrollbar(painter, kit, palette, W_SCROLL, track, scroll, self.content_height);
        }
        // Restore the card's top edge over any partially scrolled row.
    }

    fn paint_empty_list(&self, painter: &Painter<'_>, viewport: R) {
        let palette = self.palette;
        let center_y = viewport.y + viewport.h / 2.0;
        let plus = R::new(viewport.x + viewport.w / 2.0 - 23.0, center_y - 66.0, 46.0, 46.0);
        painter.fill(plus, 14.0, palette.accent_soft);
        painter.text(plus, "＋", TextStyle::new(24.0, false, palette.accent).centered());
        let title = if self.ultra { "항목이 없습니다" } else { "아직 등록된 바로가기가 없습니다" };
        let hint = if self.ultra {
            "위 버튼에서 추가하세요."
        } else {
            "위의 추가 버튼으로 첫 번째 항목을 만들어 보세요."
        };
        painter.text(
            R::new(viewport.x, center_y - 8.0, viewport.w, 22.0),
            title,
            TextStyle::new(13.0, true, palette.text).centered(),
        );
        painter.text(
            R::new(viewport.x, center_y + 16.0, viewport.w, 18.0),
            hint,
            TextStyle::new(10.0, false, palette.muted).centered(),
        );
    }

    fn paint_item_row(&mut self, painter: &mut Painter<'_>, kit: &mut Kit, index: usize, rect: R, viewport: R, visible: bool) {
        let palette = self.palette;
        let item: &LauncherItem = &self.config.items[index];
        let count = self.config.items.len();
        let base = W_ROW + index as Wid * ROW_ACTIONS;
        let can_up = item.order != 0;
        let can_down = (item.order as usize) + 1 < count;
        if !visible {
            // Tab traversal still reaches off-screen rows.
            kit.focusables.push(base);
            kit.focusables.push(base + 1);
            if can_up {
                kit.focusables.push(base + 2);
            }
            if can_down {
                kit.focusables.push(base + 3);
            }
            return;
        }
        let glyph = match item.item_type {
            ItemType::Url => GLYPH_LINK,
            ItemType::Folder => GLYPH_FOLDER,
            ItemType::File => GLYPH_FILE,
        };
        let actions_width = 48.0 + 5.0 + 34.0 + 5.0 + 34.0 + 5.0 + 34.0;
        let text_left = rect.x + 12.0 + 40.0 + 11.0;
        let text_width = if self.ultra {
            (rect.right() - text_left - 8.0).max(40.0)
        } else {
            (rect.right() - text_left - actions_width - 16.0).max(40.0)
        };
        let width_key = text_width.round() as u32;
        let graphics = painter.graphics;
        let mut fit = |text: &str, size: f32, bold: bool| -> String {
            let key = (format!("{size}{bold}\0{text}"), width_key);
            self.ellipsis
                .entry(key)
                .or_insert_with(|| graphics.ellipsize_middle(text, text_width, size, bold))
                .clone()
        };
        let name = fit(&item.name, 12.0, true);
        let path = fit(&item.path, 9.0, false);

        painter.clip(viewport, |painter| {
            painter.fill(rect, 11.0, palette.surface_alt);
            painter.stroke(rect, 11.0, 1.0, palette.border);
            let tile = R::new(rect.x + 12.0, rect.y + 11.0, 40.0, 40.0);
            painter.fill(tile, 10.0, palette.accent_soft);
            painter.text(tile, glyph, TextStyle::new(17.0, false, palette.accent).icon());
            painter.text(
                R::new(text_left, rect.y + 11.0, text_width, 22.0),
                &name,
                TextStyle::new(12.0, true, palette.text).valign(Align::End),
            );
            painter.text(
                R::new(text_left, rect.y + 34.0, text_width, 17.0),
                &path,
                TextStyle::new(9.0, false, palette.muted).valign(Align::Start),
            );
        });

        // Row actions: only fully visible rows are clickable.
        let y = if self.ultra { rect.bottom() - 40.0 } else { rect.y + (rect.h - 32.0) / 2.0 };
        let mut x = rect.right() - 8.0 - actions_width;
        let spec: [(&str, f32, ButtonStyle, bool); 4] = [
            ("수정", 48.0, ButtonStyle::ghost(palette).sized(10.0), true),
            (
                GLYPH_DELETE,
                34.0,
                ButtonStyle { text: palette.danger, hover: palette.danger_soft, ..ButtonStyle::ghost(palette) }
                    .sized(15.0)
                    .icon(),
                true,
            ),
            ("↑", 34.0, ButtonStyle::ghost(palette).sized(10.0), can_up),
            ("↓", 34.0, ButtonStyle::ghost(palette).sized(10.0), can_down),
        ];
        for (offset, (label, width, style, enabled)) in spec.into_iter().enumerate() {
            let target = R::new(x, y, width, 32.0);
            if viewport.encloses(target) {
                button(painter, kit, palette, base + offset as Wid, target, label, style, enabled);
            } else if enabled {
                kit.focusables.push(base + offset as Wid);
            }
            x += width + 5.0;
        }
    }

    /// A settings card; returns its rect. `content` lays out below the header.
    fn card_frame(&self, painter: &Painter<'_>, rect: R, title: &str, description: &str) {
        let palette = self.palette;
        painter.fill(rect, 14.0, palette.surface);
        painter.stroke(rect, 14.0, 1.0, palette.border);
        painter.text(
            R::new(rect.x + 18.0, rect.y + 16.0, rect.w - 36.0, 20.0),
            title,
            TextStyle::new(13.0, true, palette.text),
        );
        if !self.compact {
            painter.text(
                R::new(rect.x + 18.0, rect.y + 38.0, rect.w - 36.0, 18.0),
                description,
                TextStyle::new(10.0, false, palette.muted),
            );
        }
    }

    fn begin_scroll(&mut self, top: f32, left: f32, width: f32) -> R {
        let padding = self.padding();
        let viewport = R::new(
            left + padding,
            top,
            (width - padding - padding.max(14.0)).max(1.0),
            (self.height - top - padding).max(1.0),
        );
        self.viewport = viewport;
        viewport
    }

    fn finish_scroll(&mut self, painter: &Painter<'_>, kit: &mut Kit, viewport: R, content: f32) {
        self.content_height = content;
        if content > viewport.h {
            let track = R::new(viewport.right() + 2.0, viewport.y, 10.0, viewport.h);
            scrollbar(painter, kit, self.palette, W_SCROLL, track, self.scroll, content);
        }
    }

    fn paint_shortcuts_page(&mut self, painter: &mut Painter<'_>, kit: &mut Kit, left: f32, width: f32) {
        let palette = self.palette;
        let top = self.paint_page_title(
            painter,
            left,
            width,
            "단축키·시작",
            "키보드 호출 방식과 Windows 로그인 동작을 관리합니다.",
        );
        let viewport = self.begin_scroll(top, left, width);
        let mut y = viewport.y - self.scroll;
        let narrow = self.compact;

        // Keyboard shortcuts card.
        let warning = if self.warnings.is_empty() {
            None
        } else {
            Some(format!("⚠ {}", self.warnings.join(" ")))
        };
        let header = if self.compact { 42.0 } else { 62.0 };
        let wrap_width = viewport.w - 36.0;
        let warning_height = warning
            .as_ref()
            .map_or(0.0, |text| painter.graphics.measure(text, 10.0, false, wrap_width, true).1 + 8.0);
        let fields_height = if narrow {
            16.0 + 5.0 + 38.0 + 7.0 + 16.0 + 5.0 + 38.0 + 12.0 + 36.0
        } else {
            16.0 + 5.0 + 38.0 + 12.0 + 36.0
        };
        let card_height = header + fields_height + warning_height + 18.0;
        let card = R::new(viewport.x, y, viewport.w, card_height);
        let mut edit_rects = [None; 2];
        painter.clip(viewport, |painter| {
            self.card_frame(painter, card, "키보드 단축키", "다른 앱과 겹치면 수정한 뒤 적용하세요.");
        });
        let inner = R::new(card.x + 18.0, card.y + header, card.w - 36.0, fields_height);
        let field_labels = ["패널 열기", "탐색기에서 빠른 등록"];
        let ids = [W_EDIT_PANEL, W_EDIT_QUICK];
        for index in 0..2 {
            let (label_rect, field) = if narrow {
                let row_y = inner.y + index as f32 * (16.0 + 5.0 + 38.0 + 7.0);
                (
                    R::new(inner.x, row_y, inner.w, 16.0),
                    R::new(inner.x, row_y + 21.0, inner.w, 38.0),
                )
            } else {
                let column = (inner.w - 16.0) / 2.0;
                let x = inner.x + index as f32 * (column + 16.0);
                (
                    R::new(x, inner.y, column, 16.0),
                    R::new(x, inner.y + 21.0, column, 38.0),
                )
            };
            kit.focusables.push(ids[index]);
            if !viewport.encloses(field) {
                continue;
            }
            kit.hit(field, ids[index]);
            painter.text(label_rect, field_labels[index], TextStyle::new(10.0, true, palette.muted));
            painter.fill(field, 9.0, palette.surface_alt);
            let focused = kit.focus == Some(ids[index]);
            painter.stroke(field, 9.0, if focused { 2.0 } else { 1.0 }, if focused { palette.accent } else { palette.border });
            edit_rects[index] = Some(field);
        }
        let apply = if narrow {
            R::new(inner.x, inner.bottom() - 36.0, inner.w, 36.0)
        } else {
            R::new(inner.right() - 112.0, inner.bottom() - 36.0, 112.0, 36.0)
        };
        if viewport.encloses(apply) {
            button(painter, kit, palette, W_APPLY_HOTKEYS, apply, "변경사항 적용", ButtonStyle::primary(palette), true);
        } else {
            kit.focusables.push(W_APPLY_HOTKEYS);
        }
        if let Some(text) = &warning {
            let rect = R::new(inner.x, inner.bottom() + 8.0, inner.w, warning_height);
            if rect.overlaps(viewport) {
                painter.clip(viewport, |painter| {
                    painter.text(rect, text, TextStyle::new(10.0, false, palette.warning).wrapped());
                });
            }
        }
        y += card_height + 12.0;

        // Startup card.
        let card_height = if self.compact { 98.0 } else { 72.0 };
        let card = R::new(viewport.x, y, viewport.w, card_height);
        painter.clip(viewport, |painter| {
            self.card_frame(
                painter,
                card,
                "Windows 시작 시 실행",
                "로그인하면 QuickAccess를 트레이에서 자동으로 시작합니다.",
            );
        });
        let (status_text, tone) = registration_text(self.startup);
        let control_y = if self.compact { card.y + 50.0 } else { card.y + 22.0 };
        let toggle = R::new(card.right() - 18.0 - 46.0, control_y, 46.0, 28.0);
        let pill_width = painter.graphics.measure(status_text, 9.0, true, 400.0, false).0 + 16.0;
        let pill = if self.compact {
            R::new(card.x + 12.0, control_y, pill_width.min(card.w - 80.0), 28.0)
        } else {
            R::new(toggle.x - 12.0 - pill_width, control_y, pill_width, 28.0)
        };
        painter.clip(viewport, |painter| {
            painter.fill(pill, 8.0, palette.surface_alt);
            painter.text(pill, status_text, TextStyle::new(9.0, true, tone_color(tone, palette)).centered());
        });
        if viewport.encloses(toggle) {
            switch(painter, kit, palette, W_STARTUP, toggle, self.config.run_on_startup);
        } else {
            kit.focusables.push(W_STARTUP);
        }
        y += card_height;

        self.edit_rects = edit_rects;
        let content = y + self.scroll - viewport.y;
        self.finish_scroll(painter, kit, viewport, content);
    }

    fn paint_appearance_page(&mut self, painter: &mut Painter<'_>, kit: &mut Kit, left: f32, width: f32) {
        let palette = self.palette;
        let top = self.paint_page_title(
            painter,
            left,
            width,
            "화면·패널",
            "눈에 편한 스타일과 한 줄에 표시할 항목 수를 선택합니다.",
        );
        let viewport = self.begin_scroll(top, left, width);
        let mut y = viewport.y - self.scroll;
        let card_height = if self.compact { 94.0 } else { 72.0 };
        let appearance = match self.config.appearance_mode {
            AppearanceMode::System => 0,
            AppearanceMode::Light => 1,
            AppearanceMode::Dark => 2,
        };
        let rows: [(&str, &str, Wid, &[&str], usize, f32); 2] = [
            (
                "화면 스타일",
                "시스템 설정을 따르거나 밝게·어둡게 고정합니다.",
                W_APPEARANCE,
                &["시스템", "밝게", "어둡게"],
                appearance,
                198.0,
            ),
            (
                "패널 레이아웃",
                "한 줄에 표시할 바로가기 수를 선택합니다.",
                W_COLUMNS,
                &["2열", "3열", "4열", "5열"],
                (self.config.columns - 2) as usize,
                198.0,
            ),
        ];
        for (title, description, base, labels, selected, control_width) in rows {
            let card = R::new(viewport.x, y, viewport.w, card_height);
            painter.clip(viewport, |painter| self.card_frame(painter, card, title, description));
            let control = if self.compact {
                R::new(card.x + 12.0, card.y + 46.0, card.w - 24.0, 34.0)
            } else {
                R::new(card.right() - 18.0 - control_width, card.y + 19.0, control_width, 34.0)
            };
            if viewport.encloses(control) {
                segmented(painter, kit, palette, base, control, labels, selected);
            }
            y += card_height + 12.0;
        }
        let content = y - 12.0 + self.scroll - viewport.y;
        self.finish_scroll(painter, kit, viewport, content);
    }

    fn paint_about_page(&mut self, painter: &mut Painter<'_>, kit: &mut Kit, left: f32, width: f32) {
        let palette = self.palette;
        let top = self.paint_page_title(
            painter,
            left,
            width,
            "앱 정보",
            "업데이트, 설정 백업, 버전 정보를 한곳에서 확인합니다.",
        );
        let viewport = self.begin_scroll(top, left, width);
        let mut y = viewport.y - self.scroll;

        // Updates.
        let card_height = if self.compact { 84.0 } else { 72.0 };
        let card = R::new(viewport.x, y, viewport.w, card_height);
        painter.clip(viewport, |painter| {
            self.card_frame(painter, card, "업데이트 확인", "켜면 시작할 때 GitHub에서 새 버전을 확인합니다.");
        });
        let toggle = if self.compact {
            R::new(card.x + 18.0, card.y + 46.0, 46.0, 28.0)
        } else {
            R::new(card.right() - 22.0 - 46.0, card.y + 22.0, 46.0, 28.0)
        };
        if viewport.encloses(toggle) {
            switch(painter, kit, palette, W_UPDATES, toggle, self.config.check_updates);
        } else {
            kit.focusables.push(W_UPDATES);
        }
        y += card_height + 12.0;

        // Backup.
        let card_height = if self.compact { 124.0 } else { 112.0 };
        let card = R::new(viewport.x, y, viewport.w, card_height);
        painter.clip(viewport, |painter| {
            self.card_frame(painter, card, "설정 백업", "바로가기와 환경 설정을 JSON 파일로 옮길 수 있습니다.");
        });
        let default_note = ("백업 파일을 가져오거나 현재 설정을 내보냅니다.".to_string(), Tone::Muted);
        let (note, tone) = self.transfer_note.unwrap_or(&default_note);
        let (note_rect, import, export) = if self.compact {
            let half = (card.w - 28.0 - 8.0) / 2.0;
            (
                R::new(card.x + 14.0, card.y + 42.0, card.w - 28.0, 28.0),
                R::new(card.x + 14.0, card.y + 74.0, half, 34.0),
                R::new(card.x + 14.0 + half + 8.0, card.y + 74.0, half, 34.0),
            )
        } else {
            let export = R::new(card.right() - 18.0 - 88.0, card.y + 64.0, 88.0, 34.0);
            let import = R::new(export.x - 8.0 - 88.0, card.y + 64.0, 88.0, 34.0);
            (R::new(card.x + 18.0, card.y + 64.0, import.x - card.x - 30.0, 34.0), import, export)
        };
        painter.clip(viewport, |painter| {
            painter.text(note_rect, note, TextStyle::new(9.0, false, tone_color(*tone, palette)));
        });
        for (id, rect, label, style) in [
            (W_IMPORT, import, "가져오기", ButtonStyle::outline(palette).sized(10.0)),
            (W_EXPORT, export, "내보내기", ButtonStyle::primary(palette).sized(10.0)),
        ] {
            if viewport.encloses(rect) {
                button(painter, kit, palette, id, rect, label, style, true);
            } else {
                kit.focusables.push(id);
            }
        }
        y += card_height + 12.0;

        // App information and diagnostics.
        let card_height = if self.ultra {
            196.0
        } else if self.compact {
            148.0
        } else {
            122.0
        };
        let card = R::new(viewport.x, y, viewport.w, card_height);
        painter.clip(viewport, |painter| {
            self.card_frame(painter, card, "앱 정보", "현재 설치된 QuickAccess의 버전과 제작 정보를 확인합니다.");
        });
        let info = if self.ultra {
            format!("Version {APP_VERSION}\n만든 사람 {APP_AUTHOR}")
        } else {
            format!("Version {APP_VERSION}  ·  만든 사람 {APP_AUTHOR}")
        };
        let info_width = painter.graphics.measure(&info, 10.0, true, 600.0, false).0 + 22.0;
        let info_rect = if self.ultra {
            R::new(card.x + 12.0, card.y + 44.0, card.w - 24.0, 44.0)
        } else if self.compact {
            R::new(card.x + 12.0, card.y + 44.0, card.w - 24.0, 34.0)
        } else {
            R::new(card.right() - 18.0 - info_width, card.y + 16.0, info_width, 34.0)
        };
        painter.clip(viewport, |painter| {
            painter.fill(info_rect, 9.0, palette.surface_alt);
            let mut style = TextStyle::new(10.0, true, palette.text).centered();
            style.wrap = self.ultra;
            painter.text(info_rect, &info, style);
        });
        let diagnostics_y = if self.compact { info_rect.bottom() + 12.0 } else { card.y + 72.0 };
        let diagnostics = if self.ultra {
            R::new(card.x + 12.0, diagnostics_y, card.w - 24.0, 34.0)
        } else {
            R::new(card.x + (if self.compact { 12.0 } else { 18.0 }), diagnostics_y, 112.0, 34.0)
        };
        if viewport.encloses(diagnostics) {
            button(painter, kit, palette, W_DIAGNOSTICS, diagnostics, "진단 정보 복사", ButtonStyle::outline(palette).sized(10.0), true);
        } else {
            kit.focusables.push(W_DIAGNOSTICS);
        }
        let default_note = ("오류 보고에 필요한 정보를 개인정보 없이 복사합니다.".to_string(), Tone::Muted);
        let (note, tone) = self.diagnostics_note.unwrap_or(&default_note);
        let note_rect = if self.ultra {
            R::new(card.x + 12.0, diagnostics.bottom() + 6.0, card.w - 24.0, 34.0)
        } else {
            R::new(diagnostics.right() + 10.0, diagnostics.y, card.right() - diagnostics.right() - 28.0, 34.0)
        };
        painter.clip(viewport, |painter| {
            let mut style = TextStyle::new(9.0, false, tone_color(*tone, palette));
            style.wrap = self.ultra;
            painter.text(note_rect, note, style);
        });
        y += card_height + 12.0;

        // Exit.
        let card_height = if self.compact { 90.0 } else { 72.0 };
        let card = R::new(viewport.x, y, viewport.w, card_height);
        painter.clip(viewport, |painter| {
            self.card_frame(
                painter,
                card,
                "QuickAccess 종료",
                "트레이와 전역 단축키를 해제하고 프로그램을 완전히 종료합니다.",
            );
        });
        let exit = if self.compact {
            R::new(card.x + 12.0, card.y + 44.0, card.w - 24.0, 34.0)
        } else {
            R::new(card.right() - 18.0 - 116.0, card.y + 19.0, 116.0, 34.0)
        };
        if viewport.encloses(exit) {
            button(painter, kit, palette, W_EXIT, exit, "프로그램 종료", ButtonStyle::danger(palette).sized(10.0), true);
        } else {
            kit.focusables.push(W_EXIT);
        }
        y += card_height;

        let content = y + self.scroll - viewport.y;
        self.finish_scroll(painter, kit, viewport, content);
    }
}

fn settings_message(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match message {
        WM_CTLCOLOREDIT => edit_colors(wparam),
        WM_ERASEBKGND => LRESULT(1),
        WM_PAINT => run_paint(hwnd, || {
            with_app(|app| app.paint_settings());
        }),
        WM_SIZE => {
            with_app(|app| app.settings_mut().map(Settings::on_resized));
            LRESULT(0)
        }
        WM_DPICHANGED => {
            let dpi = (wparam.0 & 0xFFFF) as u32;
            // SAFETY: for this message, lParam points at a RECT from Windows.
            let suggested = unsafe { *(lparam.0 as *const RECT) };
            with_app(|app| app.settings_mut().map(|settings| settings.on_dpi_changed(dpi, &suggested)));
            LRESULT(0)
        }
        WM_GETMINMAXINFO => {
            // SAFETY: for this message, lParam points at a MINMAXINFO from
            // Windows; the DPI query only reads this window's state.
            unsafe {
                let info = &mut *(lparam.0 as *mut MINMAXINFO);
                let scale = GetDpiForWindow(hwnd).max(96) as f32 / 96.0;
                info.ptMinTrackSize.x = (320.0 * scale) as i32;
                info.ptMinTrackSize.y = (200.0 * scale) as i32;
            }
            LRESULT(0)
        }
        WM_CLOSE => {
            with_app(|app| app.settings_mut().map(Settings::hide));
            LRESULT(0)
        }
        WM_MOUSEMOVE => {
            with_app(|app| app.settings_mut().map(|settings| settings.mouse_move(lparam)));
            LRESULT(0)
        }
        WM_MOUSELEAVE => {
            with_app(|app| app.settings_mut().map(Settings::mouse_leave));
            LRESULT(0)
        }
        WM_LBUTTONDOWN => {
            with_app(|app| app.settings_mut().map(|settings| settings.mouse_down(lparam)));
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            dispatch(|app| {
                let config = app.config.clone();
                app.settings_mut().map(|settings| settings.mouse_up(lparam, &config)).unwrap_or_default()
            });
            LRESULT(0)
        }
        WM_MOUSEWHEEL => {
            with_app(|app| app.settings_mut().map(|settings| settings.wheel(wheel_delta(wparam))));
            LRESULT(0)
        }
        WM_KEYDOWN => {
            dispatch(|app| {
                let config = app.config.clone();
                app.settings_mut()
                    .map(|settings| settings.key_down(wparam.0 as u16, &config))
                    .unwrap_or_default()
            });
            LRESULT(0)
        }
        WM_COMMAND => {
            if (wparam.0 >> 16) as u32 & 0xFFFF == EN_CHANGE {
                // Programmatic refreshes arrive while the app is borrowed and
                // are dropped here, so only real typing marks the fields dirty.
                with_app(|app| app.settings_mut().map(Settings::hotkey_edited));
            }
            LRESULT(0)
        }
        // SAFETY: forwarding unmodified parameters for this window.
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    }
}

unsafe extern "system" fn settings_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    guard_panic(LRESULT(0), || settings_message(hwnd, message, wparam, lparam))
}

unsafe extern "system" fn hotkey_edit_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    guard_panic(LRESULT(0), || {
        // SAFETY: forwarding unmodified parameters to the next subclass proc.
        let forward = || unsafe { DefSubclassProc(hwnd, message, wparam, lparam) };
        match message {
            WM_KEYDOWN => {
                let vk = wparam.0 as u16;
                // SAFETY: GetKeyState only reads this thread's input state.
                let (control, shift) = unsafe {
                    (GetKeyState(VK_CONTROL.0.into()) < 0, GetKeyState(VK_SHIFT.0.into()) < 0)
                };
                if vk == VK_TAB.0 {
                    with_app(|app| app.settings_mut().map(|settings| settings.tab(shift)));
                    return LRESULT(0);
                }
                if vk == VK_ESCAPE.0 {
                    with_app(|app| app.settings_mut().map(Settings::hide));
                    return LRESULT(0);
                }
                if control && (0x31..=0x34).contains(&vk) {
                    with_app(|app| app.settings_mut().map(|settings| settings.select_page(PAGES[(vk - 0x31) as usize])));
                    return LRESULT(0);
                }
                forward()
            }
            WM_CHAR if matches!(wparam.0, 0x0D | 0x1B | 0x09) => LRESULT(0),
            WM_MOUSEWHEEL => {
                with_app(|app| app.settings_mut().map(|settings| settings.wheel(wheel_delta(wparam))));
                LRESULT(0)
            }
            _ => forward(),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_hotkeys_for_the_sidebar() {
        assert_eq!(format_hotkey("ctrl+space"), "Ctrl  +  Space");
        assert_eq!(format_hotkey("ctrl+shift+F5"), "Ctrl  +  Shift  +  F5");
    }

    #[test]
    fn presents_each_registration_state() {
        let status = |state| RegistrationStatus {
            state,
            desired_enabled: true,
            expected_command: String::new(),
            actual_command: None,
            error: None,
        };
        assert_eq!(registration_text(None).0, "실제 상태 · 확인 전");
        assert_eq!(registration_text(Some(&status(RegistrationState::Correct))).1, Tone::Accent);
        assert_eq!(registration_text(Some(&status(RegistrationState::Absent))).0, "실제 상태 · 미등록");
        assert_eq!(registration_text(Some(&status(RegistrationState::Stale))).1, Tone::Warning);
        assert_eq!(registration_text(Some(&status(RegistrationState::Unreadable))).0, "실제 상태 · 확인 불가");
    }
}
