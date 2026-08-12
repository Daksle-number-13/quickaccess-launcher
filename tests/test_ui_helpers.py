from __future__ import annotations

import unittest

from quickaccess.services.monitor import Point, Rect, Size, center_window_in_work_area
from quickaccess.ui.dialogs import position_geometry
from quickaccess.ui.popup import (
    BUTTON_HEIGHT,
    BUTTON_WIDTH,
    GAP,
    HEADER_GAP,
    HEADER_HEIGHT,
    PADDING,
    geometry_string,
    grid_navigation_target,
    popup_dimensions,
)
from quickaccess.ui.settings import settings_dimensions
from quickaccess.ui.theme import (
    ACCENT,
    BG,
    FONT_FAMILY,
    ICON_FONT_FALLBACK,
    ICON_FONT_FAMILY,
    SURFACE,
    font,
    icon_font,
)


class PopupDimensionsTests(unittest.TestCase):
    def test_formats_negative_monitor_coordinates_for_tk(self) -> None:
        self.assertEqual(
            "440x160-1920-200",
            geometry_string(440, 160, Point(-1920, -200)),
        )
        self.assertEqual(
            "440x160+20+30",
            geometry_string(440, 160, Point(20, 30)),
        )
        self.assertEqual("-1920-200", position_geometry(-1920, -200))
        self.assertEqual("+20+30", position_geometry(20, 30))

    def test_uses_configured_columns_and_grows_rows(self) -> None:
        width, height, columns, viewport = popup_dimensions(
            7,
            3,
            Rect(0, 0, 1920, 1040),
        )
        self.assertEqual(columns, 3)
        self.assertEqual(width, PADDING * 2 + 3 * BUTTON_WIDTH + 2 * GAP)
        self.assertGreater(height, 2 * BUTTON_HEIGHT)
        self.assertGreaterEqual(viewport, BUTTON_HEIGHT)

    def test_reduces_columns_when_item_count_is_smaller(self) -> None:
        _width, _height, columns, _viewport = popup_dimensions(
            1,
            5,
            Rect(-1920, 0, 0, 1080),
        )
        self.assertEqual(columns, 1)

    def test_caps_popup_to_monitor_work_area(self) -> None:
        width, height, columns, _viewport = popup_dimensions(
            100,
            5,
            Rect(0, 0, 640, 360),
        )
        self.assertLessEqual(width, 624)
        self.assertLessEqual(height, 344)
        self.assertEqual(columns, 3)

    def test_empty_state_keeps_a_compact_readable_width(self) -> None:
        width, height, columns, viewport = popup_dimensions(
            0,
            5,
            Rect(0, 0, 1920, 1040),
        )
        self.assertEqual(width, 360)
        self.assertEqual(columns, 1)
        self.assertEqual(viewport, BUTTON_HEIGHT)
        self.assertEqual(
            height,
            PADDING * 2 + HEADER_HEIGHT + HEADER_GAP + BUTTON_HEIGHT,
        )

    def test_narrow_work_area_falls_back_to_one_card_column(self) -> None:
        width, _height, columns, _viewport = popup_dimensions(
            4,
            5,
            Rect(0, 0, 250, 500),
        )
        self.assertEqual(columns, 1)
        self.assertLessEqual(width, 234)

    def test_high_dpi_popup_stays_inside_physical_work_area(self) -> None:
        width, height, columns, _viewport = popup_dimensions(
            100,
            5,
            Rect(0, 0, 2880, 1704),
            window_scaling=2.0,
        )
        self.assertLessEqual(round(width * 2.0), 2864)
        self.assertLessEqual(round(height * 2.0), 1688)
        self.assertGreaterEqual(columns, 1)

    def test_centers_physical_window_inside_negative_work_area(self) -> None:
        work_area = Rect(-1920, -120, 0, 960)
        position = center_window_in_work_area(Size(1000, 700), work_area)
        self.assertEqual(position, Point(-1460, 70))

    def test_settings_reserves_native_frame_on_small_high_dpi_display(self) -> None:
        for scale in (1.5, 2.0):
            with self.subTest(scale=scale):
                width, height = settings_dimensions(1366, 728, scale)
                self.assertLessEqual(round(width * scale) + 120, 1366)
                self.assertLessEqual(round(height * scale) + 120, 728)

    def test_settings_never_forces_a_minimum_larger_than_tiny_work_area(self) -> None:
        width, height = settings_dimensions(800, 560, 2.0)
        self.assertEqual((width, height), (340, 220))
        self.assertLessEqual(round(width * 2.0) + 120, 800)
        self.assertLessEqual(round(height * 2.0) + 120, 560)

    def test_grid_navigation_moves_within_bounds_in_a_three_column_grid(self) -> None:
        # 7 items in 3 columns: rows [0,1,2], [3,4,5], [6]
        self.assertEqual(grid_navigation_target(0, 0, "right", 3, 7), 1)
        self.assertEqual(grid_navigation_target(0, 2, "right", 3, 7), None)
        self.assertEqual(grid_navigation_target(0, 1, "left", 3, 7), 0)
        self.assertEqual(grid_navigation_target(0, 0, "left", 3, 7), None)
        self.assertEqual(grid_navigation_target(1, 0, "up", 3, 7), 0)
        self.assertEqual(grid_navigation_target(0, 0, "up", 3, 7), None)
        self.assertEqual(grid_navigation_target(0, 0, "down", 3, 7), 3)
        self.assertEqual(grid_navigation_target(1, 0, "down", 3, 7), 6)
        # Row 2 only has one card (index 6); moving down or right from it has
        # nothing to land on.
        self.assertIsNone(grid_navigation_target(2, 0, "down", 3, 7))
        self.assertIsNone(grid_navigation_target(2, 0, "right", 3, 7))

    def test_shared_theme_helpers_return_ctk_compatible_values(self) -> None:
        for color in (BG, SURFACE, ACCENT):
            self.assertEqual(len(color), 2)
            self.assertTrue(all(value.startswith("#") for value in color))
        self.assertEqual(font(13, "bold"), (FONT_FAMILY, 13, "bold"))
        family, size = icon_font(16)
        self.assertIn(family, (ICON_FONT_FAMILY, ICON_FONT_FALLBACK))
        self.assertEqual(size, 16)


if __name__ == "__main__":
    unittest.main()
