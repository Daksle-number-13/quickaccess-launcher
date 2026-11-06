//! Cursor-positioned launcher popup.
//!
//! The window stays mapped but DWM-cloaked between invocations, so showing it
//! never remaps an HWND. Layout and the first frame happen while cloaked, which
//! means the first visible frame is already at its final position and DPI.

use std::collections::HashMap;
use std::sync::Arc;

use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_CLOAK};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Controls::WM_MOUSELEAVE;
use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
use windows::Win32::UI::HiDpi::GetDpiForWindow;
use windows::Win32::UI::Input::Ime::{ImmGetCompositionStringW, ImmGetContext, ImmReleaseContext, GCS_COMPSTR};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetKeyState, ReleaseCapture, SetCapture, SetFocus, VK_CONTROL, VK_DOWN, VK_END, VK_ESCAPE, VK_F,
    VK_HOME, VK_LEFT, VK_NEXT, VK_PRIOR, VK_RETURN, VK_RIGHT, VK_SHIFT, VK_SPACE, VK_TAB, VK_UP,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, LoadCursorW, RegisterClassW,
    SetCursor, SetForegroundWindow, SetWindowPos, ShowWindow, CS_HREDRAW, CS_VREDRAW, ES_AUTOHSCROLL,
    HMENU, HTCLIENT, HWND_TOPMOST, IDC_ARROW, IDC_HAND, SWP_NOACTIVATE, SWP_NOZORDER, SW_HIDE,
    SW_SHOWNA, WINDOW_EX_STYLE, WINDOW_STYLE, WM_ACTIVATE, WM_CHAR, WM_COMMAND, WM_CTLCOLOREDIT,
    WM_DPICHANGED, WM_ERASEBKGND, WM_IME_COMPOSITION, WM_IME_ENDCOMPOSITION, WM_KEYDOWN,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEACTIVATE, WM_MOUSEMOVE, WM_MOUSEWHEEL,
    WM_PAINT, WM_RBUTTONUP, WM_SETCURSOR, WNDCLASSW, WS_CHILD, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
    WS_POPUP, WS_VISIBLE,
};

use crate::app::{dispatch, with_app, Action, App};
use crate::geometry::{
    clamp_to_work_area, grid_navigation_target, popup_dimensions, Direction, Point, PopupLayout, Rect,
    Size, BUTTON_HEIGHT, BUTTON_WIDTH, GAP, HEADER_GAP, HEADER_HEIGHT, PADDING,
};
use crate::icons::{icon_key, IconImage};
use crate::model::{ItemType, LauncherConfig, LauncherItem};
use crate::platform::MonitorContext;
use crate::pyish::wide;
use crate::search::SearchIndex;
use crate::ui::gfx::{Align, Painter, Surface, TextStyle, R};
use crate::ui::kit::{
    self, apply_round_region, apply_window_chrome, button, edit_colors, edit_text, guard_panic, physical,
    pointer, run_paint, scroll_from_pointer, scrollbar, set_cue_banner, set_edit_text, wheel_delta,
    ButtonStyle, EditFont, Kit, Wid,
};
use crate::ui::theme::{Palette, GLYPH_FILE, GLYPH_FOLDER, GLYPH_LINK, GLYPH_SETTINGS};
use crate::validation::PathStatus;

const W_SEARCH: Wid = 1;
const W_SETTINGS: Wid = 2;
const W_EMPTY_BUTTON: Wid = 3;
const W_SCROLL: Wid = 4;
const W_CARD: Wid = 1000;
const EN_CHANGE: u32 = 0x0300;
const WA_INACTIVE: u32 = 0;
const WINDOW_RADIUS: i32 = 16;
const CARD_RADIUS: f32 = 14.0;
const WHEEL_STEP: f32 = 76.0;
const ROW_PITCH: i32 = BUTTON_HEIGHT + GAP;
/// Uncloaking can deliver a delayed focus loss right after the panel opens.
pub const FOCUS_ARMING_MS: u32 = 240;

/// The state a window needs to draw, borrowed from the application.
pub struct ViewData<'a> {
    pub config: &'a LauncherConfig,
    pub statuses: &'a HashMap<String, PathStatus>,
    pub icons: &'a HashMap<String, Arc<IconImage>>,
    pub palette: &'a Palette,
}

fn status_text(item: &LauncherItem, status: Option<PathStatus>) -> &'static str {
    match status {
        Some(PathStatus::Missing) => "경로 없음 · 재지정",
        Some(PathStatus::Timeout) => "응답 지연 · 열기 시도",
        Some(PathStatus::Error) => "확인 실패 · 재지정",
        _ => match item.item_type {
            ItemType::Url => "웹 링크",
            ItemType::Folder => "폴더",
            ItemType::File => "파일",
        },
    }
}

pub fn set_cloak(hwnd: HWND, cloaked: bool) -> bool {
    let value: i32 = i32::from(cloaked);
    // SAFETY: the attribute pointer and size describe a live BOOL-sized value.
    unsafe {
        DwmSetWindowAttribute(hwnd, DWMWA_CLOAK, (&value as *const i32).cast(), 4).is_ok()
    }
}

pub struct Popup {
    pub hwnd: HWND,
    pub edit: HWND,
    surface: Surface,
    kit: Kit,
    pub visible: bool,
    cloak_available: bool,
    dpi: u32,
    layout: PopupLayout,
    edit_font: Option<EditFont>,
    edit_font_pixels: i32,
    edit_rect: Option<R>,
    index: Option<SearchIndex>,
    index_generation: u64,
    /// Positions into `config.items`, in result order.
    results: Vec<usize>,
    has_query: bool,
    scroll: f32,
    dragging_scroll: bool,
    previous_foreground: isize,
    dwm_rounded: bool,
    anchor: Point,
    work_area: Rect,
    /// Set while a context menu runs, which must not count as losing focus.
    pub suppress_deactivate: bool,
    pub show_generation: u64,
    shown_at: std::time::Instant,
}

pub fn register_class(instance: HINSTANCE) {
    let class = WNDCLASSW {
        style: CS_HREDRAW | CS_VREDRAW,
        lpfnWndProc: Some(popup_proc),
        hInstance: instance,
        lpszClassName: w!("QuickAccessPopup"),
        // SAFETY: loading a stock system cursor.
        hCursor: unsafe { LoadCursorW(None, IDC_ARROW) }.unwrap_or_default(),
        ..Default::default()
    };
    // SAFETY: the class struct refers to static strings and a valid procedure.
    unsafe { RegisterClassW(&class) };
}

impl Popup {
    pub fn create() -> windows::core::Result<Self> {
        // SAFETY: standard window creation on the UI thread; the created
        // handles are owned by this struct for the process lifetime.
        unsafe {
            let instance: HINSTANCE = GetModuleHandleW(None)?.into();
            register_class(instance);
            let hwnd = CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                w!("QuickAccessPopup"),
                w!("QuickAccess"),
                WS_POPUP,
                0,
                0,
                400,
                300,
                None,
                None,
                Some(instance),
                None,
            )?;
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
            )?;
            let _ = SetWindowSubclass(edit, Some(search_edit_proc), 1, 0);
            set_cue_banner(edit, "바로가기 검색");
            let dpi = GetDpiForWindow(hwnd).max(96);
            Ok(Self {
                hwnd,
                edit,
                surface: Surface::new(hwnd, dpi),
                kit: Kit::default(),
                visible: false,
                cloak_available: true,
                dpi,
                layout: popup_dimensions(0, 3, Rect { left: 0, top: 0, right: 1280, bottom: 720 }, 1.0),
                edit_font: None,
                edit_font_pixels: 0,
                edit_rect: None,
                index: None,
                index_generation: u64::MAX,
                results: Vec::new(),
                has_query: false,
                scroll: 0.0,
                dragging_scroll: false,
                previous_foreground: 0,
                dwm_rounded: false,
                anchor: Point { x: 0, y: 0 },
                work_area: Rect { left: 0, top: 0, right: 1280, bottom: 720 },
                suppress_deactivate: false,
                show_generation: 0,
                shown_at: std::time::Instant::now(),
            })
        }
    }

    fn scale(&self) -> f32 {
        self.dpi as f32 / 96.0
    }

    fn sync_index(&mut self, config: &LauncherConfig, generation: u64) {
        if generation != self.index_generation {
            self.index = Some(SearchIndex::new(&config.items));
            self.index_generation = generation;
            self.apply_query(&self.query_text());
        }
    }

    fn query_text(&self) -> String {
        let text = edit_text(self.edit);
        // SAFETY: the IME context is released below; the buffer size is taken
        // from the byte count the API reports.
        let composing = unsafe {
            let context = ImmGetContext(self.edit);
            if context.is_invalid() {
                String::new()
            } else {
                let bytes = ImmGetCompositionStringW(context, GCS_COMPSTR, None, 0);
                let mut value = String::new();
                if bytes > 0 {
                    let mut units = vec![0_u16; bytes as usize / 2];
                    ImmGetCompositionStringW(
                        context,
                        GCS_COMPSTR,
                        Some(units.as_mut_ptr().cast()),
                        bytes as u32,
                    );
                    value = String::from_utf16_lossy(&units);
                }
                let _ = ImmReleaseContext(self.edit, context);
                value
            }
        };
        // Some IMEs keep the composing syllable out of the control's text;
        // searching it too keeps results live while a Korean syllable forms.
        if composing.is_empty() || text.ends_with(&composing) {
            text
        } else {
            text + &composing
        }
    }

    fn apply_query(&mut self, query: &str) {
        let Some(index) = &self.index else { return };
        self.results = index.search(query);
        self.has_query = query.split(crate::pyish::is_space).any(|part| !part.is_empty());
        self.scroll = 0.0;
    }

    pub fn on_search_changed(&mut self) {
        self.apply_query(&self.query_text());
        if !matches!(self.kit.focus, Some(W_SEARCH)) {
            self.kit.focus = Some(W_SEARCH);
        }
        self.invalidate();
    }

    pub fn invalidate(&self) {
        // SAFETY: invalidating a window owned by this thread.
        unsafe {
            let _ = InvalidateRect(Some(self.hwnd), None, false);
        }
    }

    fn apply_placement(&mut self, config: &LauncherConfig, monitor_dpi: u32) {
        let scale = f64::from(monitor_dpi) / 96.0;
        self.dpi = monitor_dpi;
        self.layout = popup_dimensions(
            config.items.len() as i32,
            config.columns as i32,
            self.work_area,
            scale,
        );
        let width = (f64::from(self.layout.width) * scale).round() as i32;
        let height = (f64::from(self.layout.height) * scale).round() as i32;
        let position = clamp_to_work_area(self.anchor, Size { width, height }, self.work_area);
        // SAFETY: moving a window owned by this thread. A monitor change sends
        // WM_DPICHANGED during this call; layout is handled explicitly here.
        unsafe {
            let _ = SetWindowPos(
                self.hwnd,
                Some(HWND_TOPMOST),
                position.x,
                position.y,
                width,
                height,
                SWP_NOACTIVATE,
            );
        }
        if !self.dwm_rounded {
            apply_round_region(self.hwnd, width, height, (WINDOW_RADIUS as f32 * self.scale()) as i32);
        }
    }

    /// Stage geometry and the first frame while the window stays cloaked.
    fn layout_hidden(&mut self, data: &ViewData<'_>, generation: u64, monitor: &MonitorContext) {
        self.sync_index(data.config, generation);
        self.work_area = monitor.work_area;
        self.apply_placement(data.config, monitor.dpi);
        // SAFETY: querying a window owned by this thread.
        let actual = unsafe { GetDpiForWindow(self.hwnd) };
        if actual != 0 && actual != monitor.dpi {
            // A display-setting race: re-lay out for the DPI Windows assigned.
            self.apply_placement(data.config, actual);
        }
        self.surface.set_dpi(self.dpi);
        self.surface.sync_size();
        kit::set_edit_palette(data.palette);
        self.paint(data);
    }

    /// Build the window and pay device creation before hotkeys become active.
    pub fn prepare(&mut self, data: &ViewData<'_>, generation: u64, monitor: &MonitorContext) {
        self.dwm_rounded = apply_window_chrome(self.hwnd, data.palette, true);
        self.anchor = Point { x: monitor.work_area.left + 8, y: monitor.work_area.top + 8 };
        if set_cloak(self.hwnd, true) {
            // SAFETY: showing a cloaked window owned by this thread.
            unsafe {
                let _ = ShowWindow(self.hwnd, SW_SHOWNA);
            }
        } else {
            self.cloak_available = false;
        }
        self.layout_hidden(data, generation, monitor);
    }

    pub fn show(
        &mut self,
        data: &ViewData<'_>,
        generation: u64,
        anchor: Point,
        monitor: &MonitorContext,
    ) {
        self.previous_foreground = crate::platform::foreground_window();
        self.anchor = anchor;
        self.scroll = 0.0;
        self.kit.focus = Some(W_SEARCH);
        self.kit.hover = None;
        self.dwm_rounded = apply_window_chrome(self.hwnd, data.palette, true);
        // Search state belongs to one invocation. Reset while still hidden so
        // a reopened popup never flashes stale results.
        set_edit_text(self.edit, "");
        self.index_generation = u64::MAX;
        self.layout_hidden(data, generation, monitor);
        self.expose();
        self.visible = true;
        self.show_generation += 1;
        self.shown_at = std::time::Instant::now();
        // SAFETY: the process just received the hotkey or activation signal
        // that entitles it to take the foreground.
        unsafe {
            let _ = SetForegroundWindow(self.hwnd);
            let _ = SetFocus(Some(self.edit));
        }
    }

    fn expose(&mut self) {
        if self.cloak_available && set_cloak(self.hwnd, false) {
            return;
        }
        // A DWM reset is exceptionally rare; ordinary mapping is the safe path.
        self.cloak_available = false;
        // SAFETY: showing a window owned by this thread.
        unsafe {
            let _ = ShowWindow(self.hwnd, SW_SHOWNA);
        }
    }

    /// Hide; `restore` returns focus to the app that was active before.
    pub fn hide(&mut self, restore: bool) {
        if !self.visible {
            return;
        }
        self.visible = false;
        self.dragging_scroll = false;
        if !(self.cloak_available && set_cloak(self.hwnd, true)) {
            // SAFETY: hiding a window owned by this thread.
            unsafe {
                let _ = ShowWindow(self.hwnd, SW_HIDE);
            }
        }
        if restore && self.previous_foreground != 0 {
            // SAFETY: the handle may be stale, in which case the call fails.
            unsafe {
                let _ = SetForegroundWindow(HWND(self.previous_foreground as *mut _));
            }
        }
    }

    /// Update an open popup after a config change without moving it.
    pub fn refresh_layout(&mut self, data: &ViewData<'_>, generation: u64) {
        if !self.visible {
            return;
        }
        let monitor = MonitorContext {
            identifier: String::new(),
            bounds: self.work_area,
            work_area: self.work_area,
            dpi: self.dpi,
        };
        self.layout_hidden(data, generation, &monitor);
    }

    /// Whether a focus loss now may just be the native show transition.
    pub fn is_arming(&self) -> bool {
        self.shown_at.elapsed().as_millis() < u128::from(FOCUS_ARMING_MS)
    }

    fn viewport(&self) -> R {
        let top = (PADDING + HEADER_HEIGHT + HEADER_GAP) as f32;
        R::new(
            PADDING as f32,
            top,
            self.layout.width as f32 - PADDING as f32 * 2.0,
            self.layout.viewport_height as f32,
        )
    }

    fn rows(&self) -> i32 {
        ((self.results.len() as i32 + self.layout.columns - 1) / self.layout.columns.max(1)).max(1)
    }

    fn content_height(&self) -> f32 {
        let rows = self.rows();
        (rows * BUTTON_HEIGHT + (rows - 1).max(0) * GAP) as f32
    }

    fn max_scroll(&self) -> f32 {
        (self.content_height() - self.viewport().h).max(0.0)
    }

    fn card_rect(&self, position: usize) -> R {
        let columns = self.layout.columns.max(1) as usize;
        let (row, column) = (position / columns, position % columns);
        let viewport = self.viewport();
        R::new(
            viewport.x + (column as i32 * (BUTTON_WIDTH + GAP)) as f32,
            viewport.y + (row as i32 * ROW_PITCH) as f32 - self.scroll,
            BUTTON_WIDTH as f32,
            BUTTON_HEIGHT as f32,
        )
    }

    fn scroll_track(&self) -> R {
        let viewport = self.viewport();
        R::new(self.layout.width as f32 - 15.0, viewport.y, 12.0, viewport.h)
    }

    pub fn paint(&mut self, data: &ViewData<'_>) {
        let Self { surface, .. } = self;
        let frame = FrameState {
            layout: self.layout,
            compact: self.layout.columns == 1,
            results: self.results.clone(),
            has_query: self.has_query,
            scroll: self.scroll,
            rounded: self.dwm_rounded,
            viewport: {
                let top = (PADDING + HEADER_HEIGHT + HEADER_GAP) as f32;
                R::new(
                    PADDING as f32,
                    top,
                    self.layout.width as f32 - PADDING as f32 * 2.0,
                    self.layout.viewport_height as f32,
                )
            },
            content_height: {
                let rows = ((self.results.len() as i32 + self.layout.columns - 1)
                    / self.layout.columns.max(1))
                .max(1);
                (rows * BUTTON_HEIGHT + (rows - 1).max(0) * GAP) as f32
            },
        };
        let kit = &mut self.kit;
        let mut field = R::default();
        surface.draw(|painter| {
            field = paint_frame(painter, kit, data, &frame);
        });
        self.place_edit(field);
    }

    fn place_edit(&mut self, field: R) {
        let scale = self.scale();
        let pixels = (12.0 * scale).round() as i32;
        if self.edit_font.is_none() || self.edit_font_pixels != pixels {
            let font = EditFont::create(pixels);
            font.apply(self.edit);
            self.edit_font = Some(font);
            self.edit_font_pixels = pixels;
            self.edit_rect = None;
        }
        if self.edit_rect == Some(field) {
            return;
        }
        self.edit_rect = Some(field);
        let line = 18.0;
        let inner = R::new(field.x + 10.0, field.y + (field.h - line) / 2.0, (field.w - 18.0).max(1.0), line);
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

    pub fn mouse_move(&mut self, lparam: LPARAM) {
        let (x, y) = pointer(lparam, self.scale());
        if self.dragging_scroll {
            self.scroll = scroll_from_pointer(y, self.scroll_track(), self.content_height());
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

    pub fn wants_hand_cursor(&self) -> bool {
        matches!(self.kit.hover, Some(id) if id >= W_CARD)
    }

    pub fn mouse_down(&mut self, lparam: LPARAM) {
        let (x, y) = pointer(lparam, self.scale());
        if self.kit.mouse_down(x, y) == Some(W_SCROLL) {
            self.dragging_scroll = true;
            self.scroll = scroll_from_pointer(y, self.scroll_track(), self.content_height());
            // SAFETY: capturing the mouse for a window owned by this thread.
            unsafe {
                SetCapture(self.hwnd);
            }
            self.invalidate();
        }
    }

    pub fn mouse_up(&mut self, lparam: LPARAM, config: &LauncherConfig, statuses: &HashMap<String, PathStatus>) -> Vec<Action> {
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
            Some(W_SETTINGS | W_EMPTY_BUTTON) => {
                self.hide(false);
                vec![Action::OpenSettings]
            }
            Some(id) if id >= W_CARD => self.activate((id - W_CARD) as usize, config, statuses),
            _ => Vec::new(),
        }
    }

    pub fn context_menu(&mut self, lparam: LPARAM, config: &LauncherConfig) -> Vec<Action> {
        let (x, y) = pointer(lparam, self.scale());
        match self.kit.hit_test(x, y) {
            Some(id) if id >= W_CARD => self
                .results
                .get((id - W_CARD) as usize)
                .and_then(|index| config.items.get(*index))
                .map(|item| Action::CopyPathMenu { name: item.name.clone(), path: item.path.clone() })
                .into_iter()
                .collect(),
            _ => Vec::new(),
        }
    }

    pub fn wheel(&mut self, delta: i32) {
        let notches = delta as f32 / 120.0;
        self.scroll = (self.scroll - notches * WHEEL_STEP).clamp(0.0, self.max_scroll());
        self.invalidate();
    }

    fn activate(&mut self, position: usize, config: &LauncherConfig, statuses: &HashMap<String, PathStatus>) -> Vec<Action> {
        let Some(item) = self.results.get(position).and_then(|index| config.items.get(*index)) else {
            return Vec::new();
        };
        let id = item.id.clone();
        let broken = statuses.get(&id).is_some_and(|status| status.is_broken());
        self.hide(false);
        vec![if broken { Action::RelocateItem(id) } else { Action::ActivateItem(id) }]
    }

    fn focus_search(&mut self) {
        self.kit.focus = Some(W_SEARCH);
        // SAFETY: focusing a child of this thread's own active window.
        unsafe {
            let _ = SetFocus(Some(self.edit));
        }
        self.invalidate();
    }

    fn focus_widget(&mut self, id: Wid) {
        if id == W_SEARCH {
            self.focus_search();
            return;
        }
        self.kit.focus = Some(id);
        // SAFETY: focusing this thread's own active window.
        unsafe {
            let _ = SetFocus(Some(self.hwnd));
        }
        if id >= W_CARD {
            self.scroll_into_view((id - W_CARD) as usize);
        }
        self.invalidate();
    }

    fn scroll_into_view(&mut self, position: usize) {
        let card = self.card_rect(position);
        let viewport = self.viewport();
        if card.y < viewport.y {
            self.scroll -= viewport.y - card.y;
        } else if card.bottom() > viewport.bottom() {
            self.scroll += card.bottom() - viewport.bottom();
        }
        self.scroll = self.scroll.clamp(0.0, self.max_scroll());
    }

    fn focused_position(&self) -> Option<usize> {
        match self.kit.focus {
            Some(id) if id >= W_CARD => Some((id - W_CARD) as usize),
            _ => None,
        }
    }

    fn navigate(&mut self, position: usize, direction: Direction) {
        let columns = self.layout.columns.max(1);
        let index = position as i32;
        let target = grid_navigation_target(
            index / columns,
            index % columns,
            direction,
            columns,
            self.results.len() as i32,
        );
        match target {
            Some(target) => self.focus_widget(W_CARD + target as Wid),
            None if direction == Direction::Up && index / columns == 0 => self.focus_search(),
            None => {}
        }
    }

    fn page(&mut self, position: usize, direction: i32) {
        let columns = self.layout.columns.max(1);
        let index = position as i32;
        let rows_per_page = (self.layout.viewport_height / ROW_PITCH).max(1);
        let last_row = self.rows() - 1;
        let target_row = (index / columns + direction * rows_per_page).clamp(0, last_row);
        let target = (target_row * columns + index % columns).min(self.results.len() as i32 - 1);
        self.focus_widget(W_CARD + target.max(0) as Wid);
    }

    fn escape(&mut self) {
        if edit_text(self.edit).is_empty() {
            self.hide(true);
        } else {
            set_edit_text(self.edit, "");
            self.apply_query("");
            self.focus_search();
        }
    }

    /// Keys pressed while a card or button holds the keyboard focus.
    pub fn key_down(&mut self, vk: u16, config: &LauncherConfig, statuses: &HashMap<String, PathStatus>) -> Vec<Action> {
        // SAFETY: GetKeyState only reads this thread's input state.
        let (control, shift) = unsafe {
            (GetKeyState(VK_CONTROL.0.into()) < 0, GetKeyState(VK_SHIFT.0.into()) < 0)
        };
        if vk == VK_ESCAPE.0 {
            self.escape();
        } else if vk == VK_F.0 && control {
            self.focus_search();
        } else if vk == VK_TAB.0 {
            if let Some(next) = self.kit.next_focus(shift) {
                self.focus_widget(next);
            }
        } else if vk == VK_RETURN.0 || vk == VK_SPACE.0 {
            if self.kit.focus == Some(W_SETTINGS) || self.kit.focus == Some(W_EMPTY_BUTTON) {
                self.hide(false);
                return vec![Action::OpenSettings];
            }
            if let Some(position) = self.focused_position() {
                return self.activate(position, config, statuses);
            }
        } else if let Some(position) = self.focused_position() {
            if vk == VK_UP.0 {
                self.navigate(position, Direction::Up);
            } else if vk == VK_DOWN.0 {
                self.navigate(position, Direction::Down);
            } else if vk == VK_LEFT.0 {
                self.navigate(position, Direction::Left);
            } else if vk == VK_RIGHT.0 {
                self.navigate(position, Direction::Right);
            } else if vk == VK_HOME.0 {
                self.focus_widget(W_CARD);
            } else if vk == VK_END.0 && !self.results.is_empty() {
                self.focus_widget(W_CARD + self.results.len() as Wid - 1);
            } else if vk == VK_PRIOR.0 {
                self.page(position, -1);
            } else if vk == VK_NEXT.0 {
                self.page(position, 1);
            }
        }
        Vec::new()
    }

    /// Keys pressed in the search box. `true` when the key was consumed.
    pub fn search_key(&mut self, vk: u16, config: &LauncherConfig, statuses: &HashMap<String, PathStatus>) -> (bool, Vec<Action>) {
        // SAFETY: GetKeyState only reads this thread's input state.
        let shift = unsafe { GetKeyState(VK_SHIFT.0.into()) < 0 };
        if vk == VK_UP.0 || vk == VK_DOWN.0 {
            if !self.results.is_empty() {
                let target = if vk == VK_DOWN.0 { 0 } else { self.results.len() - 1 };
                self.focus_widget(W_CARD + target as Wid);
            }
            (true, Vec::new())
        } else if vk == VK_RETURN.0 {
            (true, self.activate(0, config, statuses))
        } else if vk == VK_ESCAPE.0 {
            self.escape();
            (true, Vec::new())
        } else if vk == VK_TAB.0 {
            if let Some(next) = self.kit.next_focus(shift) {
                self.focus_widget(next);
            }
            (true, Vec::new())
        } else {
            (false, Vec::new())
        }
    }
}

struct FrameState {
    layout: PopupLayout,
    compact: bool,
    results: Vec<usize>,
    has_query: bool,
    scroll: f32,
    rounded: bool,
    viewport: R,
    content_height: f32,
}

/// Draw one frame; returns the search field rectangle for the native edit.
fn paint_frame(painter: &mut Painter<'_>, kit: &mut Kit, data: &ViewData<'_>, frame: &FrameState) -> R {
    let palette = data.palette;
    let width = frame.layout.width as f32;
    let height = frame.layout.height as f32;
    kit.begin_frame();
    painter.clear(palette.surface);
    if !frame.rounded {
        painter.stroke(R::new(0.0, 0.0, width, height), WINDOW_RADIUS as f32, 1.0, palette.border);
    }

    let field = paint_header(painter, kit, data, frame, width);

    let viewport = frame.viewport;
    if data.config.items.is_empty() {
        let card = R::new(viewport.x, viewport.y, viewport.w, BUTTON_HEIGHT as f32);
        painter.fill(card, CARD_RADIUS, palette.surface_alt);
        painter.stroke(card, CARD_RADIUS, 1.0, palette.border);
        painter.text(
            R::new(card.x, card.y + 12.0, card.w, 24.0),
            "등록된 항목이 없습니다",
            TextStyle::new(12.0, true, palette.text).centered(),
        );
        painter.text(
            R::new(card.x, card.y + 36.0, card.w, 20.0),
            "설정에서 폴더나 파일을 추가하세요.",
            TextStyle::new(10.0, false, palette.muted).centered(),
        );
    } else if frame.results.is_empty() && frame.has_query {
        let card = R::new(viewport.x, viewport.y, viewport.w, BUTTON_HEIGHT as f32);
        painter.fill(card, CARD_RADIUS, palette.surface_alt);
        painter.stroke(card, CARD_RADIUS, 1.0, palette.border);
        painter.text(
            R::new(card.x, card.y + 6.0, card.w, 22.0),
            "검색 결과가 없습니다",
            TextStyle::new(11.0, true, palette.text).centered(),
        );
        let target = R::new(card.x + (card.w - 92.0) / 2.0, card.y + 34.0, 92.0, 26.0);
        button(
            painter,
            kit,
            palette,
            W_EMPTY_BUTTON,
            target,
            "설정에서 추가",
            ButtonStyle::soft(palette).sized(9.0).radius(8.0),
            true,
        );
    } else {
        let columns = frame.layout.columns.max(1) as usize;
        for (position, item_index) in frame.results.iter().enumerate() {
            kit.focusables.push(W_CARD + position as Wid);
            let (row, column) = (position / columns, position % columns);
            let card = R::new(
                viewport.x + (column as i32 * (BUTTON_WIDTH + GAP)) as f32,
                viewport.y + (row as i32 * ROW_PITCH) as f32 - frame.scroll,
                BUTTON_WIDTH as f32,
                BUTTON_HEIGHT as f32,
            );
            if !card.overlaps(viewport) {
                continue;
            }
            let item = &data.config.items[*item_index];
            let id = W_CARD + position as Wid;
            let visible_top = card.y.max(viewport.y);
            let visible_bottom = card.bottom().min(viewport.bottom());
            kit.hit(R::new(card.x, visible_top, card.w, visible_bottom - visible_top), id);
            paint_card(painter, kit, data, card, viewport, item, id);
        }
    }

    if frame.content_height > viewport.h && !(frame.results.is_empty()) {
        let track = R::new(width - 15.0, viewport.y, 12.0, viewport.h);
        scrollbar(painter, kit, palette, W_SCROLL, track, frame.scroll, frame.content_height);
    }
    field
}

fn paint_header(painter: &mut Painter<'_>, kit: &mut Kit, data: &ViewData<'_>, frame: &FrameState, width: f32) -> R {
    let palette = data.palette;
    let top = PADDING as f32;
    let header_height = HEADER_HEIGHT as f32;
    let left = PADDING as f32;
    let right = width - PADDING as f32;
    let settings_size = if frame.compact { 28.0 } else { 32.0 };
    let settings = R::new(right - settings_size, top + (header_height - settings_size) / 2.0, settings_size, settings_size);

    let mut field_left = left;
    let mut field_right = settings.x - 6.0;
    kit.focusables.push(W_SEARCH);
    if !frame.compact {
        let brand = painter.graphics.brand();
        painter.image("\0brand", &brand, R::new(left, top + 6.0, 30.0, 30.0));
        let name_width = painter.graphics.measure("QuickAccess", 14.0, true, 400.0, false).0.ceil();
        let badge = R::new(settings.x - 7.0 - 42.0, top + 9.0, 42.0, 24.0);
        field_right = badge.x - 8.0;
        painter.fill(badge, 12.0, palette.accent_soft);
        painter.text(
            badge,
            &format!("{}개", frame.results.len()),
            TextStyle::new(10.0, true, palette.accent).centered(),
        );
        let after_mark = left + 30.0 + 9.0;
        // Keep a usable search field on the narrowest two-column panel.
        if field_right - (after_mark + name_width + 10.0) >= 110.0 {
            painter.text(
                R::new(after_mark, top, name_width + 2.0, header_height),
                "QuickAccess",
                TextStyle::new(14.0, true, palette.text),
            );
            field_left = after_mark + name_width + 10.0;
        } else {
            field_left = after_mark;
        }
    }
    let field = R::new(field_left, top + 5.0, (field_right - field_left).max(40.0), 32.0);
    painter.fill(field, 10.0, palette.surface_alt);
    let focused = kit.focus == Some(W_SEARCH);
    painter.stroke(field, 10.0, if focused { 2.0 } else { 1.0 }, if focused { palette.accent } else { palette.border });

    let icon_size = if frame.compact { 14.0 } else { 15.0 };
    button(
        painter,
        kit,
        palette,
        W_SETTINGS,
        settings,
        GLYPH_SETTINGS,
        ButtonStyle::ghost(palette).sized(icon_size).radius(10.0).icon(),
        true,
    );
    field
}

fn paint_card(
    painter: &mut Painter<'_>,
    kit: &Kit,
    data: &ViewData<'_>,
    card: R,
    viewport: R,
    item: &LauncherItem,
    id: Wid,
) {
    let palette = data.palette;
    let status = data.statuses.get(&item.id).copied();
    let broken = status.is_some_and(PathStatus::is_broken);
    let timed_out = status == Some(PathStatus::Timeout);
    let (normal, hover, border, status_color) = if timed_out {
        (palette.warning_soft, palette.warning_soft_hover, palette.warning, palette.warning)
    } else if broken {
        (palette.danger_soft, palette.danger_soft_hover, palette.danger, palette.danger)
    } else {
        (palette.surface_alt, palette.surface_hover, palette.border, palette.muted)
    };
    let status_is_warning = broken || timed_out;
    let hovered = kit.hover == Some(id);
    let focused = kit.focus == Some(id);
    let key = icon_key(&item.path, item.item_type);
    let icon = data.icons.get(&key).cloned();

    // The clip keeps partially scrolled cards out of the header.
    let tile = R::new(card.x + 8.0, card.y + 17.0, 34.0, 34.0);
    let needs_clip = !viewport.encloses(card);
    let body = |painter: &Painter<'_>| {
        painter.fill(card, CARD_RADIUS, if hovered { hover } else { normal });
        painter.stroke(card, CARD_RADIUS, 1.0, if focused { palette.accent } else { border });
        painter.fill(tile, 10.0, if broken { palette.surface } else { palette.accent_soft });
        let name = R::new(card.x + 49.0, card.y + 11.0, card.w - 57.0, 24.0);
        painter.text(name, &item.name, TextStyle::new(11.0, true, palette.text).valign(Align::End));
        let detail = R::new(card.x + 49.0, card.y + 36.0, card.w - 53.0, 20.0);
        painter.text(
            detail,
            status_text(item, status),
            // Warning texts are longer than the type labels and must still fit.
            TextStyle::new(if status_is_warning { 9.0 } else { 10.0 }, false, status_color).valign(Align::Start),
        );
        if icon.is_none() || broken {
            let glyph = match item.item_type {
                ItemType::Url => GLYPH_LINK,
                ItemType::Folder => GLYPH_FOLDER,
                ItemType::File => GLYPH_FILE,
            };
            painter.text(tile, glyph, TextStyle::new(16.0, false, if broken { border } else { palette.accent }).icon());
        }
    };
    if needs_clip {
        painter.clip(viewport, body);
    } else {
        body(painter);
    }
    if let (Some(image), false) = (&icon, broken) {
        let target = tile.inset(5.0, 5.0);
        if viewport.encloses(target) {
            painter.image(&key, image, target);
        }
    }
}

fn popup_message(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    // SAFETY (for the DefWindowProcW calls): forwarding unmodified parameters
    // received for this window.
    match message {
        WM_CTLCOLOREDIT => edit_colors(wparam),
        WM_ERASEBKGND => LRESULT(1),
        WM_PAINT => run_paint(hwnd, || {
            with_app(|app| app.paint_popup());
        }),
        WM_MOUSEMOVE => {
            with_app(|app| app.popup.mouse_move(lparam));
            LRESULT(0)
        }
        WM_MOUSELEAVE => {
            with_app(|app| app.popup.mouse_leave());
            LRESULT(0)
        }
        WM_LBUTTONDOWN => {
            with_app(|app| app.popup.mouse_down(lparam));
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            dispatch(|app| app.popup.mouse_up(lparam, &app.config, &app.statuses));
            LRESULT(0)
        }
        WM_RBUTTONUP => {
            dispatch(|app| app.popup.context_menu(lparam, &app.config));
            LRESULT(0)
        }
        WM_MOUSEWHEEL => {
            with_app(|app| app.popup.wheel(wheel_delta(wparam)));
            LRESULT(0)
        }
        WM_KEYDOWN => {
            dispatch(|app| app.popup.key_down(wparam.0 as u16, &app.config, &app.statuses));
            LRESULT(0)
        }
        WM_COMMAND => {
            if (wparam.0 >> 16) as u32 & 0xFFFF == EN_CHANGE {
                with_app(|app| app.popup.on_search_changed());
            }
            LRESULT(0)
        }
        WM_SETCURSOR => {
            let hand = (lparam.0 & 0xFFFF) as u32 == HTCLIENT
                && with_app(|app| app.popup.wants_hand_cursor()).unwrap_or(false);
            if hand {
                // SAFETY: loading and applying a stock system cursor.
                unsafe {
                    SetCursor(LoadCursorW(None, IDC_HAND).ok());
                }
                return LRESULT(1);
            }
            unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
        }
        WM_ACTIVATE => {
            if (wparam.0 & 0xFFFF) as u32 == WA_INACTIVE {
                with_app(App::on_popup_deactivated);
            }
            LRESULT(0)
        }
        WM_MOUSEACTIVATE => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
        // Placement is explicit; the suggested rectangle must not override it.
        WM_DPICHANGED => LRESULT(0),
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    }
}

unsafe extern "system" fn popup_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    guard_panic(LRESULT(0), || popup_message(hwnd, message, wparam, lparam))
}

unsafe extern "system" fn search_edit_proc(
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
                let control = unsafe { GetKeyState(VK_CONTROL.0.into()) < 0 };
                if control && vk == VK_F.0 {
                    return LRESULT(0);
                }
                let consumed = std::cell::Cell::new(false);
                dispatch(|app| {
                    let (handled, actions) = app.popup.search_key(vk, &app.config, &app.statuses);
                    consumed.set(handled);
                    actions
                });
                if consumed.get() {
                    LRESULT(0)
                } else {
                    forward()
                }
            }
            // The matching WM_CHAR would otherwise beep in a single-line edit.
            WM_CHAR if matches!(wparam.0, 0x0D | 0x1B | 0x09) => LRESULT(0),
            WM_MOUSEWHEEL => {
                with_app(|app| app.popup.wheel(wheel_delta(wparam)));
                LRESULT(0)
            }
            WM_IME_COMPOSITION | WM_IME_ENDCOMPOSITION => {
                let result = forward();
                with_app(|app| app.popup.on_search_changed());
                result
            }
            _ => forward(),
        }
    })
}

/// Show the single-item "copy path" menu at the cursor.
pub fn run_copy_path_menu(owner: HWND, name: &str, path: &str) {
    use windows::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreatePopupMenu, DestroyMenu, TrackPopupMenu, MF_STRING, TPM_RETURNCMD, TPM_RIGHTBUTTON,
    };
    let label = wide(&format!("'{}' 경로 복사", crate::pyish::ellipsize(name, 18)));
    let Some(cursor) = crate::platform::cursor_position() else {
        return;
    };
    // SAFETY: the label outlives the calls and the menu is destroyed below.
    unsafe {
        let Ok(menu) = CreatePopupMenu() else {
            return;
        };
        let _ = AppendMenuW(menu, MF_STRING, 1, PCWSTR(label.as_ptr()));
        let command = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, cursor.x, cursor.y, None, owner, None);
        let _ = DestroyMenu(menu);
        if command.0 == 1 {
            let _ = crate::platform::copy_to_clipboard(owner, path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn describes_each_status() {
        let item = LauncherItem::new("", "x", r"C:\x", ItemType::File, 0).unwrap();
        let url = LauncherItem::new("", "x", "https://x.y", ItemType::Url, 0).unwrap();
        let folder = LauncherItem::new("", "x", r"C:\x", ItemType::Folder, 0).unwrap();
        assert_eq!(status_text(&item, Some(PathStatus::Missing)), "경로 없음 · 재지정");
        assert_eq!(status_text(&item, Some(PathStatus::Timeout)), "응답 지연 · 열기 시도");
        assert_eq!(status_text(&item, Some(PathStatus::Error)), "확인 실패 · 재지정");
        assert_eq!(status_text(&item, Some(PathStatus::Valid)), "파일");
        assert_eq!(status_text(&url, None), "웹 링크");
        assert_eq!(status_text(&folder, None), "폴더");
    }
}
