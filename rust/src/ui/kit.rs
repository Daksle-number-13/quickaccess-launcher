//! Immediate-mode widgets: each frame redraws and re-registers its hit targets.

use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Direct2D::Common::D2D1_COLOR_F;
use windows::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_USE_IMMERSIVE_DARK_MODE,
    DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND, DWM_WINDOW_CORNER_PREFERENCE,
};
use windows::Win32::Graphics::Gdi::{
    BeginPaint, CreateFontW, CreateRoundRectRgn, CreateSolidBrush, DeleteObject, EndPaint,
    SetBkColor, SetTextColor, SetWindowRgn, ANSI_CHARSET, CLEARTYPE_QUALITY, CLIP_DEFAULT_PRECIS,
    DEFAULT_PITCH, FF_DONTCARE, FW_NORMAL, HDC, HFONT, HGDIOBJ, OUT_DEFAULT_PRECIS, PAINTSTRUCT,
};
use windows::Win32::Foundation::COLORREF;
use windows::Win32::UI::Controls::{EM_SETCUEBANNER, EM_SETSEL};
use windows::Win32::UI::Input::KeyboardAndMouse::{TrackMouseEvent, TME_LEAVE, TRACKMOUSEEVENT};
use windows::Win32::UI::WindowsAndMessaging::{
    GetWindowTextLengthW, GetWindowTextW, SendMessageW, SetWindowTextW, WM_SETFONT,
};

use crate::pyish::wide;
use crate::ui::gfx::{Painter, TextStyle, R};
use crate::ui::theme::Palette;

pub type Wid = u32;

/// Hit targets and pointer state for one window.
#[derive(Default)]
pub struct Kit {
    hits: Vec<(R, Wid)>,
    pub focusables: Vec<Wid>,
    pub hover: Option<Wid>,
    pub pressed: Option<Wid>,
    pub focus: Option<Wid>,
    tracking: bool,
}

impl Kit {
    pub fn begin_frame(&mut self) {
        self.hits.clear();
        self.focusables.clear();
    }

    pub fn hit(&mut self, rect: R, id: Wid) {
        self.hits.push((rect, id));
    }

    pub fn hit_test(&self, x: f32, y: f32) -> Option<Wid> {
        self.hits
            .iter()
            .rev()
            .find(|(rect, _)| rect.contains(x, y))
            .map(|(_, id)| *id)
    }

    /// Returns whether the hover target changed and a repaint is needed.
    pub fn mouse_move(&mut self, hwnd: HWND, x: f32, y: f32) -> bool {
        if !self.tracking {
            let mut track = TRACKMOUSEEVENT {
                cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32,
                dwFlags: TME_LEAVE,
                hwndTrack: hwnd,
                dwHoverTime: 0,
            };
            // SAFETY: `track` is valid for the call and names an owned window.
            self.tracking = unsafe { TrackMouseEvent(&mut track) }.is_ok();
        }
        let hover = self.hit_test(x, y);
        let changed = hover != self.hover;
        self.hover = hover;
        changed
    }

    pub fn mouse_leave(&mut self) {
        self.tracking = false;
        self.hover = None;
    }

    pub fn mouse_down(&mut self, x: f32, y: f32) -> Option<Wid> {
        self.pressed = self.hit_test(x, y);
        self.pressed
    }

    /// The clicked widget, when the press and release landed on the same one.
    pub fn mouse_up(&mut self, x: f32, y: f32) -> Option<Wid> {
        let pressed = self.pressed.take();
        let current = self.hit_test(x, y);
        (pressed.is_some() && pressed == current).then_some(current).flatten()
    }

    /// The next focus target in tab order, wrapping at either end.
    pub fn next_focus(&self, reverse: bool) -> Option<Wid> {
        let count = self.focusables.len();
        if count == 0 {
            return None;
        }
        let current = self
            .focus
            .and_then(|focus| self.focusables.iter().position(|id| *id == focus));
        let index = match (current, reverse) {
            (Some(index), false) => (index + 1) % count,
            (Some(index), true) => (index + count - 1) % count,
            (None, false) => 0,
            (None, true) => count - 1,
        };
        Some(self.focusables[index])
    }
}

#[derive(Clone, Copy)]
pub struct ButtonStyle {
    pub fill: Option<D2D1_COLOR_F>,
    pub hover: D2D1_COLOR_F,
    pub text: D2D1_COLOR_F,
    pub border: Option<D2D1_COLOR_F>,
    pub radius: f32,
    pub size: f32,
    pub bold: bool,
    pub icon: bool,
}

impl ButtonStyle {
    pub fn primary(palette: &Palette) -> Self {
        Self {
            fill: Some(palette.accent),
            hover: palette.accent_hover,
            text: if palette.dark { palette.bg } else { palette.white },
            border: None,
            radius: 9.0,
            size: 11.0,
            bold: true,
            icon: false,
        }
    }

    pub fn soft(palette: &Palette) -> Self {
        Self {
            fill: Some(palette.accent_soft),
            hover: palette.surface_hover,
            text: palette.accent,
            ..Self::primary(palette)
        }
    }

    pub fn outline(palette: &Palette) -> Self {
        Self {
            fill: Some(palette.surface_alt),
            hover: palette.surface_hover,
            text: palette.text,
            border: Some(palette.border),
            ..Self::primary(palette)
        }
    }

    pub fn ghost(palette: &Palette) -> Self {
        Self {
            fill: None,
            hover: palette.surface_hover,
            text: palette.muted,
            ..Self::primary(palette)
        }
    }

    pub fn danger(palette: &Palette) -> Self {
        Self {
            fill: Some(palette.danger),
            hover: palette.danger_hover,
            text: if palette.dark { palette.bg } else { palette.white },
            ..Self::primary(palette)
        }
    }

    pub fn sized(mut self, size: f32) -> Self {
        self.size = size;
        self
    }

    pub fn radius(mut self, radius: f32) -> Self {
        self.radius = radius;
        self
    }

    pub fn icon(mut self) -> Self {
        self.icon = true;
        self
    }
}

pub fn button(
    painter: &Painter<'_>,
    kit: &mut Kit,
    palette: &Palette,
    id: Wid,
    rect: R,
    label: &str,
    style: ButtonStyle,
    enabled: bool,
) {
    if enabled {
        kit.hit(rect, id);
        kit.focusables.push(id);
    }
    let hovered = enabled && kit.hover == Some(id);
    let background = if hovered { Some(style.hover) } else { style.fill };
    if let Some(color) = background {
        painter.fill(rect, style.radius, color);
    }
    if let Some(border) = style.border {
        painter.stroke(rect, style.radius, 1.0, border);
    }
    if kit.focus == Some(id) {
        painter.stroke(rect, style.radius, 2.0, palette.accent);
    }
    let mut color = style.text;
    if !enabled {
        color.a = 0.35;
    }
    let mut text = TextStyle::new(style.size, style.bold && !style.icon, color).centered();
    if style.icon {
        text = text.icon();
    }
    painter.text(rect, label, text);
}

pub fn switch(painter: &Painter<'_>, kit: &mut Kit, palette: &Palette, id: Wid, rect: R, on: bool) {
    kit.hit(rect, id);
    kit.focusables.push(id);
    let track = R::new(rect.x, rect.y + (rect.h - 22.0) / 2.0, 46.0, 22.0);
    let hovered = kit.hover == Some(id);
    let color = match (on, hovered) {
        (true, true) => palette.accent_hover,
        (true, false) => palette.accent,
        (false, true) => palette.muted,
        (false, false) => palette.border,
    };
    painter.fill(track, 11.0, color);
    let knob_x = if on { track.right() - 19.0 } else { track.x + 3.0 };
    painter.fill(R::new(knob_x, track.y + 3.0, 16.0, 16.0), 8.0, palette.white);
    if kit.focus == Some(id) {
        painter.stroke(track.inset(-2.0, -2.0), 13.0, 2.0, palette.accent);
    }
}

/// A row of mutually exclusive options; widget ids are `base + index`.
pub fn segmented(
    painter: &Painter<'_>,
    kit: &mut Kit,
    palette: &Palette,
    base: Wid,
    rect: R,
    labels: &[&str],
    selected: usize,
) {
    painter.fill(rect, 9.0, palette.surface_alt);
    let width = rect.w / labels.len() as f32;
    for (index, label) in labels.iter().enumerate() {
        let id = base + index as Wid;
        let cell = R::new(rect.x + width * index as f32, rect.y, width, rect.h).inset(2.0, 2.0);
        kit.hit(cell, id);
        kit.focusables.push(id);
        let active = index == selected;
        let hovered = kit.hover == Some(id);
        if active {
            painter.fill(cell, 7.0, if hovered { palette.accent_hover } else { palette.accent });
        } else if hovered {
            painter.fill(cell, 7.0, palette.surface_hover);
        }
        if kit.focus == Some(id) {
            painter.stroke(cell, 7.0, 2.0, palette.accent);
        }
        let color = if !active {
            palette.text
        } else if palette.dark {
            palette.bg
        } else {
            palette.white
        };
        painter.text(cell, label, TextStyle::new(11.0, true, color).centered());
    }
}

/// A thin scrollbar track; returns the thumb rect for drag hit-testing.
pub fn scrollbar(
    painter: &Painter<'_>,
    kit: &mut Kit,
    palette: &Palette,
    id: Wid,
    track: R,
    scroll: f32,
    content: f32,
) -> R {
    let thumb_h = (track.h * track.h / content).max(24.0).min(track.h);
    let range = (content - track.h).max(1.0);
    let thumb_y = track.y + (track.h - thumb_h) * (scroll / range).clamp(0.0, 1.0);
    let thumb = R::new(track.x + 3.0, thumb_y, track.w - 6.0, thumb_h);
    kit.hit(track, id);
    let color = if kit.hover == Some(id) || kit.pressed == Some(id) {
        palette.muted
    } else {
        palette.border
    };
    painter.fill(thumb, (track.w - 6.0) / 2.0, color);
    thumb
}

/// The scroll offset that centers the pointer's thumb position while dragging.
pub fn scroll_from_pointer(y: f32, track: R, content: f32) -> f32 {
    let thumb_h = (track.h * track.h / content).max(24.0).min(track.h);
    let usable = (track.h - thumb_h).max(1.0);
    let fraction = ((y - track.y - thumb_h / 2.0) / usable).clamp(0.0, 1.0);
    fraction * (content - track.h).max(0.0)
}

// ---- Native helpers shared by every window ---------------------------------

thread_local! {
    static EDIT_STYLE: std::cell::Cell<(u32, u32, isize)> = const { std::cell::Cell::new((0, 0xFFFFFF, 0)) };
    static EDIT_BRUSHES: std::cell::RefCell<std::collections::HashMap<u32, isize>> =
        std::cell::RefCell::new(std::collections::HashMap::new());
}

/// Choose the colors native edit controls paint with. Stored outside the app
/// state because `WM_CTLCOLOREDIT` can arrive while that state is borrowed.
pub fn set_edit_palette(palette: &Palette) {
    let background = crate::ui::theme::colorref(palette.surface_alt);
    let text = crate::ui::theme::colorref(palette.text);
    let brush = EDIT_BRUSHES.with(|brushes| {
        *brushes.borrow_mut().entry(background).or_insert_with(|| {
            // SAFETY: the brush is cached for the process lifetime.
            unsafe { CreateSolidBrush(COLORREF(background)).0 as isize }
        })
    });
    EDIT_STYLE.with(|style| style.set((text, background, brush)));
}

/// The `WM_CTLCOLOREDIT` response for every edit control in the app.
pub fn edit_colors(wparam: WPARAM) -> LRESULT {
    let (text, background, brush) = EDIT_STYLE.with(std::cell::Cell::get);
    let dc = HDC(wparam.0 as *mut _);
    // SAFETY: `dc` is the control's device context for this paint message.
    unsafe {
        SetTextColor(dc, COLORREF(text));
        SetBkColor(dc, COLORREF(background));
    }
    LRESULT(brush)
}

/// Convert a panic at the window-procedure boundary into a log entry, since
/// unwinding across the Win32 callback would abort the resident process.
pub fn guard_panic(fallback: LRESULT, body: impl FnOnce() -> LRESULT) -> LRESULT {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(body)).unwrap_or(fallback)
}

pub fn run_paint(hwnd: HWND, paint: impl FnOnce()) -> LRESULT {
    let mut info = PAINTSTRUCT::default();
    // SAFETY: Begin/EndPaint are paired for a window owned by this thread.
    unsafe {
        BeginPaint(hwnd, &mut info);
        paint();
        let _ = EndPaint(hwnd, &info);
    }
    LRESULT(0)
}

pub fn pointer(lparam: LPARAM, scale: f32) -> (f32, f32) {
    let x = (lparam.0 & 0xFFFF) as u16 as i16;
    let y = ((lparam.0 >> 16) & 0xFFFF) as u16 as i16;
    (f32::from(x) / scale, f32::from(y) / scale)
}

pub fn wheel_delta(wparam: WPARAM) -> i32 {
    i32::from(((wparam.0 >> 16) & 0xFFFF) as u16 as i16)
}

/// Rounded corners and a theme-matched caption. `true` when DWM rounds them.
pub fn apply_window_chrome(hwnd: HWND, palette: &Palette, rounded: bool) -> bool {
    let dark: i32 = i32::from(palette.dark);
    // SAFETY: attribute pointers and sizes describe valid local values.
    unsafe {
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            (&dark as *const i32).cast(),
            4,
        );
        if !rounded {
            return false;
        }
        let corner: DWM_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND;
        let result = DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            (&corner as *const DWM_WINDOW_CORNER_PREFERENCE).cast(),
            4,
        );
        if result.is_ok() {
            let border = COLORREF(crate::ui::theme::colorref(palette.border));
            let _ = DwmSetWindowAttribute(
                hwnd,
                DWMWA_BORDER_COLOR,
                (&border as *const COLORREF).cast(),
                4,
            );
        }
        result.is_ok()
    }
}

/// Windows 10 fallback: clip the window itself to rounded corners.
pub fn apply_round_region(hwnd: HWND, width: i32, height: i32, radius: i32) {
    // SAFETY: on success the window takes ownership of the region.
    unsafe {
        let region = CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2);
        SetWindowRgn(hwnd, Some(region), true);
    }
}

pub struct EditFont(pub HFONT);

impl EditFont {
    pub fn create(pixels: i32) -> Self {
        let family = wide("Malgun Gothic");
        // SAFETY: the family name is NUL-terminated and outlives the call.
        let font = unsafe {
            CreateFontW(
                -pixels,
                0,
                0,
                0,
                FW_NORMAL.0 as i32,
                0,
                0,
                0,
                ANSI_CHARSET,
                OUT_DEFAULT_PRECIS,
                CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY,
                (DEFAULT_PITCH.0 | FF_DONTCARE.0) as u32,
                windows::core::PCWSTR(family.as_ptr()),
            )
        };
        Self(font)
    }

    pub fn apply(&self, edit: HWND) {
        // SAFETY: WM_SETFONT only stores the handle, which outlives the edit
        // because each owner drops this after re-assigning or destroying it.
        unsafe {
            SendMessageW(edit, WM_SETFONT, Some(WPARAM(self.0 .0 as usize)), Some(LPARAM(1)));
        }
    }
}

impl Drop for EditFont {
    fn drop(&mut self) {
        // SAFETY: this wrapper exclusively owns the font handle.
        unsafe {
            let _ = DeleteObject(HGDIOBJ(self.0 .0));
        }
    }
}

pub fn edit_text(edit: HWND) -> String {
    // SAFETY: the buffer is sized from the reported length plus the NUL.
    unsafe {
        let length = GetWindowTextLengthW(edit);
        if length <= 0 {
            return String::new();
        }
        let mut buffer = vec![0_u16; length as usize + 1];
        let copied = GetWindowTextW(edit, &mut buffer);
        String::from_utf16_lossy(&buffer[..copied.max(0) as usize])
    }
}

pub fn set_edit_text(edit: HWND, text: &str) {
    let units = wide(text);
    // SAFETY: the string is NUL-terminated and copied by the call.
    unsafe {
        let _ = SetWindowTextW(edit, windows::core::PCWSTR(units.as_ptr()));
    }
}

pub fn select_all(edit: HWND) {
    // SAFETY: EM_SETSEL takes plain integers.
    unsafe {
        SendMessageW(edit, EM_SETSEL, Some(WPARAM(0)), Some(LPARAM(-1)));
    }
}

pub fn set_cue_banner(edit: HWND, text: &str) {
    let units = wide(text);
    // SAFETY: the control copies the string during the call.
    unsafe {
        SendMessageW(
            edit,
            EM_SETCUEBANNER,
            Some(WPARAM(1)),
            Some(LPARAM(units.as_ptr() as isize)),
        );
    }
}

pub fn physical(rect: R, scale: f32) -> RECT {
    RECT {
        left: (rect.x * scale).round() as i32,
        top: (rect.y * scale).round() as i32,
        right: (rect.right() * scale).round() as i32,
        bottom: (rect.bottom() * scale).round() as i32,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kit_with(ids: &[Wid]) -> Kit {
        let mut kit = Kit::default();
        for (index, id) in ids.iter().enumerate() {
            kit.hit(R::new(index as f32 * 10.0, 0.0, 10.0, 10.0), *id);
            kit.focusables.push(*id);
        }
        kit
    }

    #[test]
    fn later_hits_win() {
        let mut kit = kit_with(&[1]);
        kit.hit(R::new(0.0, 0.0, 10.0, 10.0), 2);
        assert_eq!(kit.hit_test(5.0, 5.0), Some(2));
        assert_eq!(kit.hit_test(50.0, 5.0), None);
    }

    #[test]
    fn click_requires_press_and_release_on_the_same_widget() {
        let mut kit = kit_with(&[1, 2]);
        assert_eq!(kit.mouse_down(5.0, 5.0), Some(1));
        assert_eq!(kit.mouse_up(5.0, 5.0), Some(1));
        kit.mouse_down(5.0, 5.0);
        assert_eq!(kit.mouse_up(15.0, 5.0), None);
        assert_eq!(kit.mouse_up(5.0, 5.0), None);
    }

    #[test]
    fn tab_order_wraps_both_ways() {
        let mut kit = kit_with(&[1, 2, 3]);
        assert_eq!(kit.next_focus(false), Some(1));
        assert_eq!(kit.next_focus(true), Some(3));
        kit.focus = Some(3);
        assert_eq!(kit.next_focus(false), Some(1));
        kit.focus = Some(1);
        assert_eq!(kit.next_focus(true), Some(3));
        kit.focus = Some(99);
        assert_eq!(kit.next_focus(false), Some(1));
        assert_eq!(Kit::default().next_focus(false), None);
    }

    #[test]
    fn dragging_maps_to_the_full_scroll_range() {
        let track = R::new(0.0, 100.0, 10.0, 200.0);
        assert_eq!(scroll_from_pointer(0.0, track, 1000.0), 0.0);
        assert_eq!(scroll_from_pointer(400.0, track, 1000.0), 800.0);
        assert_eq!(scroll_from_pointer(150.0, track, 100.0), 0.0);
    }
}
