from __future__ import annotations

import sys
import tkinter as tk
import time
import unittest
from unittest.mock import patch

import customtkinter as ctk
from PIL import Image

import quickaccess.ui.dialogs as dialogs_module
from quickaccess.ui.dialogs import TextInputDialog, ToastManager
from quickaccess.models import LauncherConfig, LauncherItem
from quickaccess.services.icons import icon_key
from quickaccess.services.monitor import Point, Rect
from quickaccess.services.validation import PathStatus
from quickaccess.ui.popup import PopupActions, PopupPanel
from quickaccess.ui.settings import SettingsActions, SettingsWindow


@unittest.skipUnless(sys.platform == "win32", "Windows GUI smoke test")
class UiRuntimeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = ctk.CTk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(
                f"interactive Windows desktop unavailable: {error}"
            ) from error

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "root"):
            try:
                cls.root.destroy()
            except tk.TclError:
                pass

    def test_toast_can_be_constructed_and_rendered(self) -> None:
        toast = ToastManager(self.root)

        toast.show("UI 상태를 확인했습니다.", kind="success", duration_ms=1000)
        self.root.update()

        self.assertIsNotNone(toast._window)
        assert toast._window is not None
        self.assertTrue(toast._window.winfo_viewable())
        labels = self._descendants_of_type(toast._window, ctk.CTkLabel)
        self.assertIn("UI 상태를 확인했습니다.", [label.cget("text") for label in labels])
        toast.close()

    def test_settings_ultra_compact_layout_can_be_rendered(self) -> None:
        config = LauncherConfig.default()
        window = SettingsWindow(
            self.root,
            SettingsActions(
                get_config=lambda: config,
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=lambda _mode: True,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_update_checks=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )

        window._apply_compact_layout(340)
        window.geometry("340x220+0+0")
        window.deiconify()
        window.update_idletasks()
        self.root.update()

        self.assertTrue(window._ultra_compact)
        self.assertEqual(int(window._add_folder_button.grid_info()["row"]), 1)
        self.assertEqual(int(window._add_file_button.grid_info()["row"]), 1)
        self.assertEqual(int(window._add_link_button.grid_info()["row"]), 1)
        window._select_page("preferences")
        deadline = time.monotonic() + 0.5
        while not window.winfo_viewable() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(window.winfo_viewable())
        client_right = window.winfo_rootx() + window.winfo_width()
        for widget in (
            window._preferences_page,
            window._preferences_scroll,
            window._appearance,
            window._columns,
            window._panel_hotkey,
            window._quick_hotkey,
            window._startup,
            window._updates,
        ):
            self.assertLessEqual(
                widget.winfo_rootx() + widget.winfo_width(),
                client_right,
                widget.winfo_class(),
            )

        window._apply_compact_layout(1000)
        self.root.update()
        self.assertFalse(window._ultra_compact)
        self.assertEqual(int(window._add_folder_button.grid_info()["row"]), 0)
        self.assertEqual(int(window._add_link_button.grid_info()["row"]), 0)
        window.destroy()

    def test_settings_long_korean_and_unc_text_stays_inside_compact_list(self) -> None:
        config = LauncherConfig(
            items=[
                LauncherItem(
                    name="가나다라마바사아자차카타파하" * 4,
                    path=(
                        r"\\server\매우 긴 공유 폴더 이름\또 매우 긴 하위 폴더 이름"
                        r"\검사 결과 파일 이름도 매우 깁니다.xlsx"
                    ),
                    type="file",
                    order=0,
                )
            ]
        )
        window = SettingsWindow(
            self.root,
            SettingsActions(
                get_config=lambda: config,
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=lambda _mode: True,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_update_checks=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )
        try:
            window._apply_compact_layout(340)
            window.geometry("340x220+0+0")
            window.deiconify()
            self.root.update()

            canvas = window._list._parent_canvas
            card = window._list.winfo_children()[0]
            self.assertLessEqual(
                card.winfo_rootx() + card.winfo_width(),
                canvas.winfo_rootx() + canvas.winfo_width(),
            )
            self.assertEqual((0.0, 1.0), canvas.xview())
            labels = self._descendants_of_type(card, ctk.CTkLabel)
            self.assertTrue(any("…" in str(label.cget("text")) for label in labels))
        finally:
            window.destroy()

    def test_settings_keyboard_controls_and_dirty_hotkey_are_preserved(self) -> None:
        config = LauncherConfig.default()
        applied_hotkeys: list[tuple[str, str]] = []

        def set_appearance(mode: str) -> bool:
            config.set_appearance_mode(mode)
            return True

        def set_hotkeys(panel: str, quick: str) -> bool:
            applied_hotkeys.append((panel, quick))
            config.hotkey = panel
            config.quick_add_hotkey = quick
            config.normalize()
            return True

        window = SettingsWindow(
            self.root,
            SettingsActions(
                get_config=lambda: config,
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=set_appearance,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_update_checks=lambda _enabled: True,
                set_hotkeys=set_hotkeys,
            ),
        )
        try:
            window.deiconify()
            self.root.update()
            for button in (
                window._nav_buttons["items"],
                window._nav_buttons["preferences"],
                window._add_folder_button,
                window._add_file_button,
                window._add_link_button,
            ):
                self.assertEqual("1", str(button._canvas.cget("takefocus")))
                self.assertTrue(button._canvas.bind("<Return>"))
                self.assertTrue(button._canvas.bind("<space>"))

            # The binding above delegates to the same public invoke path used
            # here; synthetic key events require a real foreground desktop.
            window._nav_buttons["preferences"].invoke()
            self.root.update()
            self.assertEqual("preferences", window._active_page)
            self.assertGreaterEqual(len(window._keyboard_targets()), 10)

            window._replace_entry(window._panel_hotkey, "ctrl+alt+p")
            window._hotkey_edited()
            window._appearance_changed("밝게")
            self.assertEqual("ctrl+alt+p", window._panel_hotkey.get())
            self.assertTrue(window._hotkeys_dirty)

            window._apply_hotkeys()
            self.assertEqual(
                [("ctrl+alt+p", config.quick_add_hotkey)],
                applied_hotkeys,
            )
            self.assertFalse(window._hotkeys_dirty)
        finally:
            window.destroy()

    def test_text_input_dialog_fits_small_high_dpi_work_area(self) -> None:
        class SmallMonitor:
            def get_cursor_position(self) -> Point:
                return Point(400, 280)

            def get_monitor_work_area(self, _point: Point) -> Rect:
                return Rect(0, 0, 800, 560)

        with (
            patch.object(dialogs_module, "NativeMonitorService", SmallMonitor),
            patch.object(TextInputDialog, "_get_window_scaling", return_value=2.0),
        ):
            dialog = TextInputDialog(
                self.root,
                title="웹 링크 추가",
                prompt="인터넷 주소를 입력하세요.",
                initial_value="https://",
            )
            try:
                self.root.update()
                self.assertGreaterEqual(dialog.winfo_rootx(), 0)
                self.assertGreaterEqual(dialog.winfo_rooty(), 0)
                self.assertLessEqual(dialog.winfo_rootx() + dialog.winfo_width(), 800)
                self.assertLessEqual(dialog.winfo_rooty() + dialog.winfo_height(), 560)
                self.assertEqual("1", str(dialog._entry._entry.cget("takefocus")))
                self.assertEqual("1", str(dialog._cancel_button._canvas.cget("takefocus")))
                self.assertEqual("1", str(dialog._confirm_button._canvas.cget("takefocus")))
            finally:
                dialog.destroy()

    def test_settings_mouse_wheel_reaches_the_last_preference_card(self) -> None:
        config = LauncherConfig.default()
        window = SettingsWindow(
            self.root,
            SettingsActions(
                get_config=lambda: config,
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=lambda _mode: True,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_update_checks=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )
        window.geometry("760x520+0+0")
        window._select_page("preferences")
        window.deiconify()
        self.root.update()
        canvas = window._preferences_scroll._parent_canvas
        start = canvas.yview()

        for _ in range(12):
            window._startup_card.event_generate("<MouseWheel>", delta=-120)
        self.root.update()

        self.assertGreater(canvas.yview()[0], start[0])
        self.assertGreaterEqual(canvas.yview()[1], 0.99)
        content_bounds = canvas.bbox("all")
        assert content_bounds is not None
        self.assertGreaterEqual(
            canvas.canvasy(canvas.winfo_height()),
            content_bounds[3] - 12,
        )
        window.destroy()

    def test_settings_appearance_control_uses_persisted_value(self) -> None:
        config = LauncherConfig.default()
        selected: list[str] = []

        def set_mode(mode: str) -> bool:
            selected.append(mode)
            config.set_appearance_mode(mode)
            return True

        window = SettingsWindow(
            self.root,
            SettingsActions(
                get_config=lambda: config,
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=set_mode,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_update_checks=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )
        window._appearance_changed("밝게")

        self.assertEqual(["light"], selected)
        self.assertEqual("light", config.appearance_mode)
        self.assertEqual("밝게", window._appearance.get())
        window.destroy()

    def test_popup_reuses_render_tree_until_visible_content_changes(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        anchor = Point(20, 20)
        work_area = Rect(0, 0, 1920, 1040)

        popup.show(config, {}, anchor, work_area)
        first_shell = popup.winfo_children()[0]
        first_cards = tuple(popup._cards)
        first_render_count = popup.render_count
        popup.hide()
        popup.show(
            config,
            {item.id: PathStatus.VALID for item in config.items},
            anchor,
            work_area,
        )
        self.assertIs(first_shell, popup.winfo_children()[0])
        self.assertEqual(first_render_count, popup.render_count)

        # CustomTkinter scales the existing widget tree after WM_DPICHANGED.
        # A monitor-scale change must not rebuild identical cards.
        original_get_scaling = popup._get_window_scaling
        try:
            popup._get_window_scaling = lambda: 1.5
            popup.prepare(config, {}, work_area)
        finally:
            popup._get_window_scaling = original_get_scaling
        self.assertIs(first_shell, popup.winfo_children()[0])

        untouched_card = popup._cards[-1]
        with patch.object(
            untouched_card,
            "update_state",
            wraps=untouched_card.update_state,
        ) as untouched_update:
            popup.show(
                config,
                {config.items[0].id: PathStatus.MISSING},
                anchor,
                work_area,
            )
            untouched_update.assert_not_called()
        self.assertIs(first_shell, popup.winfo_children()[0])
        self.assertEqual(first_cards, tuple(popup._cards))
        self.assertEqual(first_render_count, popup.render_count)
        self.assertGreaterEqual(popup.dynamic_update_count, 1)
        popup._cards[0]._invoke()
        popup.hide()
        # Let the short focus/DPI settle callbacks observe the hidden state
        # before destroying this Toplevel.  This keeps callbacks from one
        # Tcl interpreter from leaking into the next runtime smoke test.
        self.root.after(300, self.root.quit)
        self.root.mainloop()
        popup.destroy()

    def test_popup_regrids_one_two_three_columns_without_rebuilding(self) -> None:
        config = LauncherConfig(
            columns=3,
            items=[
                LauncherItem(
                    name=f"항목 {index + 1}",
                    path=rf"C:\QuickAccess\Item-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(12)
            ],
        )
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        wide = Rect(0, 0, 1920, 1040)
        two_columns = Rect(0, 0, 330, 1040)
        one_column = Rect(0, 0, 180, 1040)
        try:
            with patch.object(popup, "_get_window_scaling", return_value=1.0):
                popup.prepare(config, {}, wide)
                first_shell = popup.winfo_children()[0]
                first_frame = popup._items_frame
                first_cards = tuple(popup._cards)
                first_render_count = popup.render_count
                first_brand_mark = popup._brand_mark
                first_count_badge = popup._count_badge
                first_settings_button = popup._settings_button
                settings_return_binding = popup._settings_button._canvas.bind("<Return>")
                self.assertEqual(3, popup._layout_columns)
                wide_position = popup._cards[2].place_info()
                self.assertGreater(int(wide_position["x"]), 0)
                self.assertEqual(0, int(wide_position["y"]))
                self.assertEqual("grid", popup._count_badge.winfo_manager())

                popup.prepare(config, {}, two_columns)
                self.assertEqual(2, popup._layout_columns)
                two_column_position = popup._cards[2].place_info()
                self.assertEqual(0, int(two_column_position["x"]))
                self.assertGreater(int(two_column_position["y"]), 0)
                with patch.object(popup, "_focus_card") as focus_card:
                    popup._navigate(0, "down")
                    focus_card.assert_called_once_with(2)

                popup.prepare(config, {}, one_column)
                self.assertEqual(1, popup._layout_columns)
                one_column_position = popup._cards[2].place_info()
                self.assertEqual(0, int(one_column_position["x"]))
                self.assertGreater(
                    int(one_column_position["y"]),
                    int(two_column_position["y"]),
                )
                self.assertFalse(popup._count_badge.winfo_manager())
                self.assertEqual(24, int(popup._brand_mark.cget("width")))
                with patch.object(popup, "_focus_card") as focus_card:
                    popup._navigate(0, "down")
                    focus_card.assert_called_once_with(1)

                popup.prepare(config, {}, wide)
                self.assertEqual(3, popup._layout_columns)
                restored_position = popup._cards[2].place_info()
                self.assertGreater(int(restored_position["x"]), 0)
                self.assertEqual(0, int(restored_position["y"]))
                self.assertEqual("grid", popup._count_badge.winfo_manager())
                self.assertEqual(30, int(popup._brand_mark.cget("width")))

            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertIs(first_frame, popup._items_frame)
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(first_render_count, popup.render_count)
            self.assertIs(first_brand_mark, popup._brand_mark)
            self.assertIs(first_count_badge, popup._count_badge)
            self.assertIs(first_settings_button, popup._settings_button)
            self.assertEqual(
                settings_return_binding,
                popup._settings_button._canvas.bind("<Return>"),
            )
        finally:
            popup.destroy()

    def test_popup_reflows_between_synthetic_100_and_200_percent_dpi(self) -> None:
        config = LauncherConfig(
            columns=3,
            items=[
                LauncherItem(
                    name=f"DPI 항목 {index + 1}",
                    path=rf"C:\QuickAccess\Dpi-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(12)
            ],
        )
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        work_area = Rect(0, 0, 900, 700)
        try:
            with patch.object(popup, "_get_window_scaling", return_value=1.0):
                popup.prepare(config, {}, work_area)
            first_shell = popup.winfo_children()[0]
            first_frame = popup._items_frame
            first_cards = tuple(popup._cards)
            first_render_count = popup.render_count
            self.assertEqual(3, popup._layout_columns)
            self.assertFalse(popup._scrolling)

            with patch.object(popup, "_get_window_scaling", return_value=2.0):
                popup.prepare(config, {}, work_area)
            self.assertEqual(2, popup._layout_columns)
            self.assertTrue(popup._scrolling)
            self.assertEqual("grid", popup._items_frame._scrollbar.winfo_manager())

            with patch.object(popup, "_get_window_scaling", return_value=1.0):
                popup.prepare(config, {}, work_area)
            self.assertEqual(3, popup._layout_columns)
            self.assertFalse(popup._scrolling)
            self.assertFalse(popup._items_frame._scrollbar.winfo_manager())
            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertIs(first_frame, popup._items_frame)
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(first_render_count, popup.render_count)
        finally:
            popup.destroy()

    def test_popup_resizes_place_container_when_widget_scaling_changes(self) -> None:
        config = LauncherConfig(
            columns=2,
            items=[
                LauncherItem(
                    name=f"배율 항목 {index + 1}",
                    path=rf"C:\QuickAccess\Scale-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(12)
            ],
        )
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        previous_scaling = ctk.ScalingTracker.widget_scaling
        try:
            work_area = Rect(0, 0, 1920, 500)
            popup.prepare(config, {}, work_area)
            first_frame = popup._items_frame
            first_cards = tuple(popup._cards)
            first_render_count = popup.render_count

            ctk.set_widget_scaling(previous_scaling * 1.5)
            self.root.update()
            popup.prepare(config, {}, work_area)
            self.root.update()

            last_card_bottom = max(
                card.winfo_y() + card.winfo_height() for card in popup._cards
            )
            self.assertGreaterEqual(popup._items_frame.winfo_height(), last_card_bottom)
            self.assertIs(first_frame, popup._items_frame)
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(first_render_count, popup.render_count)
        finally:
            ctk.set_widget_scaling(previous_scaling)
            self.root.update()
            popup.destroy()

    def test_popup_scrollbar_toggles_without_replacing_cards(self) -> None:
        config = LauncherConfig(
            columns=5,
            items=[
                LauncherItem(
                    name=f"스크롤 항목 {index + 1}",
                    path=rf"C:\QuickAccess\Scroll-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(20)
            ],
        )
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        tall = Rect(0, 0, 1920, 1040)
        short = Rect(0, 0, 1920, 300)
        try:
            with patch.object(popup, "_get_window_scaling", return_value=1.0):
                popup.prepare(config, {}, tall)
                first_shell = popup.winfo_children()[0]
                first_frame = popup._items_frame
                first_cards = tuple(popup._cards)
                first_render_count = popup.render_count
                self.assertFalse(popup._scrolling)
                self.assertFalse(popup._items_frame._scrollbar.winfo_manager())

                popup.show(config, {}, Point(20, 20), short)
                self.root.update()
                self.assertTrue(popup._scrolling)
                self.assertEqual("grid", popup._items_frame._scrollbar.winfo_manager())
                canvas = popup._scroll_canvas
                self.assertIsNotNone(canvas)
                popup._focus_card(len(popup._cards) - 1)
                self.root.update()
                self.assertGreater(canvas.yview()[0], 0.0)

                popup.hide()
                popup.show(config, {}, Point(20, 20), short)
                self.assertAlmostEqual(0.0, canvas.yview()[0], places=3)
                popup.hide()
                popup.prepare(config, {}, tall)
                popup.show(config, {}, Point(20, 20), tall)
                self.root.update()
                self.assertFalse(popup._scrolling)
                self.assertFalse(popup._items_frame._scrollbar.winfo_manager())
                self.assertEqual((0.0, 1.0), popup._items_frame._parent_canvas.yview())

            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertIs(first_frame, popup._items_frame)
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(first_render_count, popup.render_count)
        finally:
            popup.hide()
            self.root.after(300, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_empty_state_uses_stable_non_scrolling_container(self) -> None:
        config = LauncherConfig(columns=3, items=[])
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            with patch.object(popup, "_get_window_scaling", return_value=1.0):
                popup.prepare(config, {}, Rect(0, 0, 1920, 1040))
            self.assertEqual([], popup._cards)
            self.assertIsNotNone(popup._empty_state)
            self.assertEqual("place", popup._empty_state.winfo_manager())
            self.assertFalse(popup._scrolling)
            self.assertFalse(popup._items_frame._scrollbar.winfo_manager())
            labels = self._descendants_of_type(popup._empty_state, ctk.CTkLabel)
            self.assertIn(
                "등록된 항목이 없습니다",
                [label.cget("text") for label in labels],
            )
            self.assertEqual(1, popup.render_count)
        finally:
            popup.destroy()

    def test_popup_structural_refresh_does_not_leak_global_wheel_bindings(self) -> None:
        def binding_count(script: str) -> int:
            return sum(bool(line.strip()) for line in script.splitlines())

        baseline = self.root.bind_all("<MouseWheel>") or ""
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            popup.prepare(config, {}, Rect(0, 0, 1920, 1040))
            expected_count = binding_count(baseline) + 1
            self.assertEqual(
                expected_count,
                binding_count(self.root.bind_all("<MouseWheel>") or ""),
            )
            for index in range(4):
                config.items[0].name = f"구조 변경 {index}"
                popup.prepare(config, {}, Rect(0, 0, 1920, 1040))
                self.assertEqual(
                    expected_count,
                    binding_count(self.root.bind_all("<MouseWheel>") or ""),
                )
        finally:
            popup.destroy()
        self.assertEqual(baseline, self.root.bind_all("<MouseWheel>") or "")

    def test_runtime_appearance_change_recolors_without_rebuilding_popup(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        anchor = Point(20, 20)
        work_area = Rect(0, 0, 1920, 1040)
        previous_mode = ctk.get_appearance_mode()
        try:
            ctk.set_appearance_mode("Light")
            popup.show(config, {}, anchor, work_area)
            shell = popup.winfo_children()[0]
            light_color = shell._apply_appearance_mode(shell.cget("fg_color"))

            ctk.set_appearance_mode("Dark")
            dark_color = shell._apply_appearance_mode(shell.cget("fg_color"))
            popup.hide()
            popup.show(config, {}, anchor, work_area)

            self.assertNotEqual(light_color, dark_color)
            self.assertIs(shell, popup.winfo_children()[0])
        finally:
            popup.hide()
            ctk.set_appearance_mode(previous_mode)
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_arrow_keys_move_focus_between_cards(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            popup.show(config, {}, Point(20, 20), Rect(0, 0, 1920, 1040))
            self.root.update()
            self.assertEqual(len(popup._cards), len(config.items))

            popup._cards[0].focus_set()
            self.root.update()
            popup._navigate(0, "right")
            self.root.update()
            self.assertIs(popup.focus_get(), popup._cards[1]._canvas)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_keyboard_focus_scrolls_and_supports_page_navigation(self) -> None:
        config = LauncherConfig(
            columns=4,
            items=[
                LauncherItem(
                    name=f"항목 {index + 1}",
                    path=rf"C:\QuickAccess\Item-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(20)
            ],
        )
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            popup.show(config, {}, Point(20, 20), Rect(0, 0, 640, 360))
            self.root.update()
            canvas = popup._scroll_canvas
            self.assertIsNotNone(canvas)
            assert canvas is not None

            popup._focus_card(len(popup._cards) - 1)
            self.root.update()
            self.assertGreater(canvas.yview()[0], 0.0)
            self.assertIs(popup.focus_get(), popup._cards[-1]._canvas)

            popup._focus_card(0)
            self.root.update()
            self.assertAlmostEqual(0.0, canvas.yview()[0], places=3)
            popup._page_navigate(0, 1)
            self.root.update()
            self.assertIsNot(popup.focus_get(), popup._cards[0]._canvas)
            self.assertTrue(popup._cards[0]._canvas.bind("<Home>"))
            self.assertTrue(popup._cards[0]._canvas.bind("<End>"))
            self.assertTrue(popup._cards[0]._canvas.bind("<Prior>"))
            self.assertTrue(popup._cards[0]._canvas.bind("<Next>"))
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_timeout_card_attempts_open_while_missing_card_relocates(self) -> None:
        config = LauncherConfig.default()
        activated: list[str] = []
        relocated: list[str] = []
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=activated.append,
                relocate=relocated.append,
                open_settings=lambda: None,
            ),
        )
        try:
            first = config.items[0]
            popup.show(
                config,
                {first.id: PathStatus.TIMEOUT},
                Point(20, 20),
                Rect(0, 0, 1920, 1040),
            )
            self.root.update()
            labels = self._descendants_of_type(popup._cards[0], ctk.CTkLabel)
            self.assertIn(
                "응답 지연 · 열기 시도",
                [label.cget("text") for label in labels],
            )
            popup._cards[0]._invoke()
            self.assertEqual([first.id], activated)
            self.assertEqual([], relocated)

            popup.show(
                config,
                {first.id: PathStatus.MISSING},
                Point(20, 20),
                Rect(0, 0, 1920, 1040),
            )
            self.root.update()
            popup._cards[0]._invoke()
            self.assertEqual([first.id], activated)
            self.assertEqual([first.id], relocated)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_context_menu_copies_path_to_clipboard(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            popup.show(config, {}, Point(20, 20), Rect(0, 0, 1920, 1040))
            self.root.update()
            popup._copy_path(config.items[0].path)
            self.root.update()
            self.assertEqual(popup.clipboard_get(), config.items[0].path)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_renders_a_ready_icon_image_instead_of_the_glyph(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            pixels = bytes([10, 20, 30, 255] * (24 * 24))
            pil_image = Image.frombuffer("RGBA", (24, 24), pixels, "raw", "BGRA", 0, 1)
            icon = ctk.CTkImage(light_image=pil_image, dark_image=pil_image, size=(24, 24))
            key = icon_key(config.items[0].path, config.items[0].type)

            work_area = Rect(0, 0, 1920, 1040)
            popup.prepare(config, {}, work_area)
            first_shell = popup.winfo_children()[0]
            first_cards = tuple(popup._cards)
            first_render_count = popup.render_count
            first_card = popup._cards[0]
            first_command = first_card._command
            with (
                patch.object(
                    first_card,
                    "configure",
                    wraps=first_card.configure,
                ) as card_configure,
                patch.object(
                    first_card._icon_tile,
                    "configure",
                    wraps=first_card._icon_tile.configure,
                ) as tile_configure,
                patch.object(
                    first_card._icon_label,
                    "configure",
                    wraps=first_card._icon_label.configure,
                ) as icon_configure,
                patch.object(
                    first_card._name_label,
                    "configure",
                    wraps=first_card._name_label.configure,
                ) as name_configure,
                patch.object(
                    first_card._status_label,
                    "configure",
                    wraps=first_card._status_label.configure,
                ) as status_configure,
            ):
                popup.prepare(config, {}, work_area, icons={key: icon})
                icon_configure.assert_called_once()
                card_configure.assert_not_called()
                tile_configure.assert_not_called()
                name_configure.assert_not_called()
                status_configure.assert_not_called()
            self.assertIs(first_command, first_card._command)
            popup.show(
                config,
                {},
                Point(20, 20),
                work_area,
                icons={key: icon},
            )
            self.root.update()

            self.assertIs(popup._cards[0]._icon_label.cget("image"), icon)
            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(first_render_count, popup.render_count)

            popup.show(
                config,
                {config.items[0].id: PathStatus.MISSING},
                Point(20, 20),
                work_area,
                icons={key: icon},
            )
            self.assertIsNone(popup._cards[0]._icon_label.cget("image"))
            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertEqual(first_render_count, popup.render_count)

            popup.show(
                config,
                {},
                Point(20, 20),
                work_area,
                icons={key: icon},
            )
            self.assertIs(popup._cards[0]._icon_label.cget("image"), icon)
            self.assertIs(first_shell, popup.winfo_children()[0])
            self.assertEqual(first_render_count, popup.render_count)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    @staticmethod
    def _descendants_of_type(parent: tk.Misc, widget_type: type) -> list:
        result = []
        pending = list(parent.winfo_children())
        while pending:
            widget = pending.pop()
            if isinstance(widget, widget_type):
                result.append(widget)
            pending.extend(widget.winfo_children())
        return result


if __name__ == "__main__":
    unittest.main()
