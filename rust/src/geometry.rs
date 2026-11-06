//! Pure window geometry. Coordinates may be negative on secondary monitors.

pub const BUTTON_WIDTH: i32 = 136;
pub const BUTTON_HEIGHT: i32 = 68;
pub const GAP: i32 = 8;
pub const PADDING: i32 = 12;
pub const HEADER_HEIGHT: i32 = 42;
pub const HEADER_GAP: i32 = 10;
pub const SCROLLBAR_RESERVE: i32 = 16;

pub const SETTINGS_WIDTH: i32 = 1000;
pub const SETTINGS_HEIGHT: i32 = 700;
const WINDOW_FRAME_BUDGET: i32 = 120;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Point {
    pub x: i32,
    pub y: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Size {
    pub width: i32,
    pub height: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rect {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

impl Rect {
    pub fn width(self) -> i32 {
        self.right - self.left
    }

    pub fn height(self) -> i32 {
        self.bottom - self.top
    }
}

/// Clamp a cursor-anchored window's top-left corner to a monitor work area.
///
/// An oversized window is anchored at the work area's top or left; the caller
/// then constrains or scrolls its contents.
pub fn clamp_to_work_area(cursor: Point, window: Size, work_area: Rect) -> Point {
    let max_x = work_area.left.max(work_area.right - window.width);
    let max_y = work_area.top.max(work_area.bottom - window.height);
    Point {
        x: cursor.x.max(work_area.left).min(max_x),
        y: cursor.y.max(work_area.top).min(max_y),
    }
}

/// A centered position, clamped when the window is oversized.
pub fn center_in_work_area(window: Size, work_area: Rect) -> Point {
    let x = work_area.left + ((work_area.width() - window.width) / 2).max(0);
    let y = work_area.top + ((work_area.height() - window.height) / 2).max(0);
    clamp_to_work_area(Point { x, y }, window, work_area)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PopupLayout {
    pub width: i32,
    pub height: i32,
    pub columns: i32,
    pub viewport_height: i32,
}

fn ceil_div(value: i32, divisor: i32) -> i32 {
    (value + divisor - 1).div_euclid(divisor)
}

fn items_height(rows: i32) -> i32 {
    rows * BUTTON_HEIGHT + (rows - 1).max(0) * GAP
}

/// Logical popup dimensions constrained to a physical work area.
pub fn popup_dimensions(
    item_count: i32,
    configured_columns: i32,
    work_area: Rect,
    scaling: f64,
) -> PopupLayout {
    let scale = scaling.max(0.1);
    let available_width = (f64::from(work_area.width() - 16) / scale).trunc().max(1.0) as i32;
    let width_limited = ((available_width - PADDING * 2 + GAP).div_euclid(BUTTON_WIDTH + GAP)).max(1);
    let mut columns = configured_columns.min(item_count.max(1)).min(width_limited).max(1);
    let natural_width = |columns: i32| PADDING * 2 + columns * BUTTON_WIDTH + (columns - 1) * GAP;
    let mut width = natural_width(columns).min(available_width);
    if item_count == 0 {
        width = width.max(360).min(available_width);
    }

    let chrome = PADDING * 2 + HEADER_HEIGHT + HEADER_GAP;
    let mut rows = ceil_div(item_count, columns).max(1);
    let mut natural_items = items_height(rows);
    let max_height = (f64::from(work_area.height() - 16) / scale).trunc().max(100.0) as i32;
    let mut height = (chrome + natural_items).min(max_height);
    let mut viewport = (height - chrome).max(BUTTON_HEIGHT);
    if natural_items > viewport {
        // A visible scrollbar consumes horizontal space. At the monitor-width
        // cap it cannot widen the window and would clip the last card column,
        // so recalculate the column count against the usable width first.
        let scroll_limited = ((available_width - SCROLLBAR_RESERVE - PADDING * 2 + GAP)
            .div_euclid(BUTTON_WIDTH + GAP))
        .max(1);
        if columns > scroll_limited {
            columns = scroll_limited;
            rows = ceil_div(item_count, columns).max(1);
            natural_items = items_height(rows);
            width = natural_width(columns).min(available_width);
            height = (chrome + natural_items).min(max_height);
            viewport = (height - chrome).max(BUTTON_HEIGHT);
        }
        width = (width + SCROLLBAR_RESERVE).min(available_width);
    }
    PopupLayout {
        width,
        height,
        columns,
        viewport_height: viewport,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Left,
    Right,
    Up,
    Down,
}

/// The row-major card index reached from `(row, column)`; `None` at a grid edge.
pub fn grid_navigation_target(
    row: i32,
    column: i32,
    direction: Direction,
    columns: i32,
    item_count: i32,
) -> Option<i32> {
    let index = row * columns + column;
    match direction {
        Direction::Left => (column > 0).then_some(index - 1),
        Direction::Right => {
            let target = index + 1;
            (column < columns - 1 && target < item_count).then_some(target)
        }
        Direction::Up => (row > 0).then_some(index - columns),
        Direction::Down => {
            let target = index + columns;
            (target < item_count).then_some(target)
        }
    }
}

/// Logical settings client size with room for the native window frame.
pub fn settings_dimensions(work_width: i32, work_height: i32, scale: f64) -> (i32, i32) {
    let safe = scale.max(0.1);
    let available_width = (f64::from(work_width - WINDOW_FRAME_BUDGET) / safe).trunc().max(1.0) as i32;
    let available_height = (f64::from(work_height - WINDOW_FRAME_BUDGET) / safe).trunc().max(1.0) as i32;
    (SETTINGS_WIDTH.min(available_width), SETTINGS_HEIGHT.min(available_height))
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect { left: 0, top: 0, right: 1920, bottom: 1040 };

    #[test]
    fn clamps_inside_negative_coordinate_monitors() {
        let area = Rect { left: -1920, top: -200, right: 0, bottom: 880 };
        let size = Size { width: 300, height: 200 };
        assert_eq!(
            clamp_to_work_area(Point { x: -5, y: 870 }, size, area),
            Point { x: -300, y: 680 }
        );
        assert_eq!(
            clamp_to_work_area(Point { x: -3000, y: -900 }, size, area),
            Point { x: -1920, y: -200 }
        );
    }

    #[test]
    fn oversized_windows_anchor_at_the_origin() {
        let area = Rect { left: 100, top: 100, right: 200, bottom: 200 };
        let size = Size { width: 500, height: 500 };
        assert_eq!(clamp_to_work_area(Point { x: 150, y: 150 }, size, area), Point { x: 100, y: 100 });
        assert_eq!(center_in_work_area(size, area), Point { x: 100, y: 100 });
    }

    #[test]
    fn centers_in_the_work_area() {
        let area = Rect { left: -1000, top: 0, right: 0, bottom: 500 };
        assert_eq!(
            center_in_work_area(Size { width: 200, height: 100 }, area),
            Point { x: -600, y: 200 }
        );
    }

    #[test]
    fn uses_natural_size_for_a_small_grid() {
        let layout = popup_dimensions(6, 3, AREA, 1.0);
        assert_eq!(layout.columns, 3);
        assert_eq!(layout.width, 12 * 2 + 3 * 136 + 2 * 8);
        assert_eq!(layout.height, 12 * 2 + 42 + 10 + 2 * 68 + 8);
    }

    #[test]
    fn narrows_columns_to_the_item_count() {
        assert_eq!(popup_dimensions(2, 5, AREA, 1.0).columns, 2);
        assert_eq!(popup_dimensions(1, 3, AREA, 1.0).columns, 1);
    }

    #[test]
    fn empty_panel_keeps_a_minimum_width() {
        let layout = popup_dimensions(0, 3, AREA, 1.0);
        assert_eq!(layout.width, 360);
        assert_eq!(layout.viewport_height, BUTTON_HEIGHT);
    }

    #[test]
    fn scaling_shrinks_the_logical_budget() {
        let small = Rect { left: 0, top: 0, right: 816, bottom: 616 };
        let layout = popup_dimensions(20, 5, small, 2.0);
        assert_eq!(layout.columns, 2);
        assert!(layout.width <= 400);
        assert!(layout.height <= 300);
    }

    #[test]
    fn scrolling_reserves_a_scrollbar() {
        let short = Rect { left: 0, top: 0, right: 1920, bottom: 300 };
        let layout = popup_dimensions(20, 3, short, 1.0);
        assert_eq!(layout.width, 12 * 2 + 3 * 136 + 2 * 8 + SCROLLBAR_RESERVE);
        assert_eq!(layout.height, 284);
        assert_eq!(layout.viewport_height, 284 - 76);
    }

    #[test]
    fn scrollbar_at_width_cap_drops_a_column_instead_of_clipping() {
        // Exactly two columns fit without a scrollbar, but not with one.
        let area = Rect { left: 0, top: 0, right: 12 * 2 + 2 * 136 + 8 + 16, bottom: 300 };
        let layout = popup_dimensions(40, 5, area, 1.0);
        assert_eq!(layout.columns, 1);
        assert!(layout.width <= area.width() - 16);
    }

    #[test]
    fn navigates_a_grid_without_wrapping() {
        use Direction::*;
        assert_eq!(grid_navigation_target(0, 0, Left, 3, 7), None);
        assert_eq!(grid_navigation_target(0, 1, Left, 3, 7), Some(0));
        assert_eq!(grid_navigation_target(0, 2, Right, 3, 7), None);
        assert_eq!(grid_navigation_target(2, 0, Right, 3, 7), None);
        assert_eq!(grid_navigation_target(0, 0, Up, 3, 7), None);
        assert_eq!(grid_navigation_target(1, 2, Up, 3, 7), Some(2));
        assert_eq!(grid_navigation_target(1, 0, Down, 3, 7), Some(6));
        assert_eq!(grid_navigation_target(1, 1, Down, 3, 7), None);
    }

    #[test]
    fn settings_fit_small_and_scaled_displays() {
        assert_eq!(settings_dimensions(1920, 1040, 1.0), (1000, 700));
        assert_eq!(settings_dimensions(1280, 720, 2.0), (580, 300));
        assert_eq!(settings_dimensions(100, 100, 1.0), (1, 1));
    }
}
