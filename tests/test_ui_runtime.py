from __future__ import annotations

import sys
import tkinter as tk
import time
import unittest
from unittest.mock import patch

import customtkinter as ctk
from PIL import Image

from quickaccess import __author__, __version__
from quickaccess.app import QuickAccessApp
import quickaccess.ui.dialogs as dialogs_module
from quickaccess.ui.dialogs import TextInputDialog, ToastManager
from quickaccess.models import LauncherConfig, LauncherItem
from quickaccess.services.icons import icon_key
from quickaccess.services.monitor import MonitorContext, Point, Rect
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

        window._apply_compact_layout(320)
        window.geometry("320x420+0+0")
        window.deiconify()
        window.update_idletasks()
        self.root.update()

        self.assertTrue(window._ultra_compact)
        self.assertEqual("canvas", window._preferences_scroll.winfo_manager())
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
            window._app_info_details,
        ):
            self.assertLessEqual(
                widget.winfo_rootx() + widget.winfo_width(),
                client_right,
                widget.winfo_class(),
            )
        self.assertEqual(2, int(window._app_info_details.grid_info()["row"]))
        self.assertEqual(2, int(window._app_info_details.grid_info()["columnspan"]))
        self.assertEqual(
            f"Version {__version__}\n만든 사람 {__author__}",
            window._app_info_details.cget("text"),
        )

        window._select_page("about")
        window.update_idletasks()
        window._about_scroll._parent_canvas.yview_moveto(1.0)
        self.root.update()
        canvas_right = (
            window._about_scroll._parent_canvas.winfo_rootx()
            + window._about_scroll._parent_canvas.winfo_width()
        )
        for widget in (
            window._transfer_actions,
            window._import_button,
            window._export_button,
            window._diagnostics_actions,
            window._diagnostics_button,
        ):
            self.assertLessEqual(
                widget.winfo_rootx() + widget.winfo_width(),
                canvas_right,
                widget.winfo_class(),
            )

        window._apply_compact_layout(1000)
        self.root.update()
        self.assertFalse(window._ultra_compact)
        self.assertEqual("canvas", window._preferences_scroll.winfo_manager())
        self.assertEqual(int(window._add_folder_button.grid_info()["row"]), 0)
        self.assertEqual(int(window._add_link_button.grid_info()["row"]), 0)
        self.assertEqual(0, int(window._app_info_details.grid_info()["row"]))
        self.assertEqual(
            f"Version {__version__}  ·  만든 사람 {__author__}",
            window._app_info_details.cget("text"),
        )
        window.destroy()

    def test_settings_displays_app_version_and_creator(self) -> None:
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
        try:
            labels = self._descendants_of_type(window._app_info_card, ctk.CTkLabel)
            texts = [str(label.cget("text")) for label in labels]
            self.assertIn("앱 정보", texts)
            self.assertIn(
                f"Version {__version__}  ·  만든 사람 {__author__}",
                texts,
            )
        finally:
            window.destroy()

    def test_settings_v2_navigation_exposes_four_focused_sections(self) -> None:
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
        try:
            self.assertEqual(
                ("items", "shortcuts", "appearance", "about"),
                tuple(window._pages),
            )
            self.assertEqual(tuple(window._pages), tuple(window._nav_buttons))
            for page_name in window._pages:
                window._nav_buttons[page_name].invoke()
                self.root.update()
                self.assertEqual(page_name, window._active_page)
                self.assertEqual("grid", window._pages[page_name].winfo_manager())
                for other_name, page in window._pages.items():
                    if other_name != page_name:
                        self.assertEqual("", page.winfo_manager())

            # Older callers that used the former page name still land in the
            # new keyboard/startup section instead of raising an error.
            window._select_page("preferences")
            self.assertEqual("shortcuts", window._active_page)
        finally:
            window.destroy()

    def test_settings_actual_startup_status_and_transfer_callbacks_are_optional(self) -> None:
        class Status:
            state = "stale"

        config = LauncherConfig.default()
        imported: list[bool] = []
        exported: list[bool] = []
        copied: list[bool] = []

        def copy_diagnostics() -> bool:
            copied.append(True)
            return len(copied) == 1

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
                get_startup_status=lambda: Status(),
                import_config=lambda: not imported.append(True),
                export_config=lambda: not exported.append(True),
                copy_diagnostics=copy_diagnostics,
            ),
        )
        fallback = SettingsWindow(
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
            self.assertEqual("실제 상태 · 이전 경로", window._startup_status.cget("text"))
            self.assertEqual("normal", window._import_button.cget("state"))
            self.assertEqual("normal", window._export_button.cget("state"))
            self.assertEqual("normal", window._diagnostics_button.cget("state"))
            self.assertEqual(
                "1", str(window._diagnostics_button._canvas.cget("takefocus"))
            )
            self.assertTrue(window._diagnostics_button._canvas.bind("<FocusIn>"))
            self.assertTrue(window._diagnostics_button._canvas.bind("<FocusOut>"))
            window._import_button.invoke()
            window._export_button.invoke()
            window._diagnostics_button.invoke()
            self.assertEqual([True], imported)
            self.assertEqual([True], exported)
            self.assertEqual([True], copied)
            self.assertEqual("설정을 내보냈습니다.", window._transfer_hint.cget("text"))
            self.assertEqual(
                "진단 정보를 클립보드에 복사했습니다.",
                window._diagnostics_feedback.cget("text"),
            )
            window._diagnostics_button.invoke()
            self.assertEqual([True, True], copied)
            self.assertEqual(
                "진단 정보가 복사되지 않았습니다.",
                window._diagnostics_feedback.cget("text"),
            )

            self.assertEqual("실제 상태 · 확인 전", fallback._startup_status.cget("text"))
            self.assertEqual("disabled", fallback._import_button.cget("state"))
            self.assertEqual("disabled", fallback._export_button.cget("state"))
            self.assertEqual("disabled", fallback._diagnostics_button.cget("state"))
        finally:
            window.destroy()
            fallback.destroy()

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
                window._nav_buttons["shortcuts"],
                window._add_folder_button,
                window._add_file_button,
                window._add_link_button,
            ):
                self.assertEqual("1", str(button._canvas.cget("takefocus")))
                self.assertTrue(button._canvas.bind("<Return>"))
                self.assertTrue(button._canvas.bind("<space>"))

            # The binding above delegates to the same public invoke path used
            # here; synthetic key events require a real foreground desktop.
            window._nav_buttons["shortcuts"].invoke()
            self.root.update()
            self.assertEqual("shortcuts", window._active_page)
            self.assertGreaterEqual(len(window._keyboard_targets()), 8)
            for entry in (window._panel_hotkey, window._quick_hotkey):
                self.assertEqual("1", str(entry._entry.cget("takefocus")))
                self.assertTrue(entry._entry.bind("<FocusIn>"))
                self.assertTrue(entry._entry.bind("<FocusOut>"))

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
        window.geometry("760x320+0+0")
        window._select_page("about")
        window.deiconify()
        self.root.update()
        canvas = window._about_scroll._parent_canvas
        start = canvas.yview()

        for _ in range(24):
            window._app_info_card.event_generate("<MouseWheel>", delta=-120)
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

    def test_prepared_popup_reuses_a_cloaked_mapping_without_ghost_window(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        work_area = Rect(0, 0, 1920, 1040)
        try:
            popup.prepare(config, {}, work_area)
            self.root.update()
            if not popup.warm_mapping_enabled:
                self.skipTest("DWM application cloaking is unavailable")

            self.assertTrue(popup.winfo_ismapped())
            self.assertFalse(popup.visible)
            self.assertIs(popup._is_native_cloaked(), True)

            transitions: list[str] = []
            popup.bind("<Map>", lambda _event: transitions.append("map"), add="+")
            popup.bind("<Unmap>", lambda _event: transitions.append("unmap"), add="+")
            render_count = popup.render_count
            cards = tuple(popup._cards)

            with patch.object(
                popup,
                "deiconify",
                wraps=popup.deiconify,
            ) as deiconify, patch.object(
                popup,
                "update_idletasks",
                wraps=popup.update_idletasks,
            ) as flush_geometry:
                popup.show(config, {}, Point(20, 20), work_area)
                self.root.update()
                self.assertTrue(popup.visible)
                self.assertIs(popup._is_native_cloaked(), False)
                self.assertIs(popup.focus_get(), popup._search_entry._entry)
                popup.hide()
                self.root.update()
                self.assertFalse(popup.visible)
                self.assertTrue(popup.winfo_ismapped())
                self.assertIs(popup._is_native_cloaked(), True)

                flush_geometry.reset_mock()
                popup.show(config, {}, Point(400, 240), work_area)
                flush_geometry.assert_called()
                self.assertRegex(popup.geometry(), r"\+400\+240$")
                self.root.update()
                self.assertTrue(popup.visible)
                self.assertIs(popup._is_native_cloaked(), False)

            deiconify.assert_not_called()
            # DWM may synthesize a Tk <Map> notification when an already
            # mapped window is uncloaked, but there must be no native unmap
            # (and therefore no expensive remap on the next invocation).
            self.assertNotIn("unmap", transitions)
            self.assertEqual(render_count, popup.render_count)
            self.assertEqual(cards, tuple(popup._cards))
        finally:
            popup.hide()
            self.root.update()
            popup.destroy()

    def test_popup_uses_withdraw_fallback_when_native_cloak_is_unavailable(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        work_area = Rect(0, 0, 1920, 1040)
        try:
            with patch.object(popup, "_set_native_cloak", return_value=False):
                popup.prepare(config, {}, work_area)
                self.assertFalse(popup.warm_mapping_enabled)
                with (
                    patch.object(
                        popup,
                        "deiconify",
                        wraps=popup.deiconify,
                    ) as deiconify,
                    patch.object(
                        popup,
                        "withdraw",
                        wraps=popup.withdraw,
                    ) as withdraw,
                ):
                    popup.show(config, {}, Point(20, 20), work_area)
                    self.root.update()
                    popup.hide()
                    self.root.update()

                deiconify.assert_called_once()
                withdraw.assert_called_once()
                self.assertFalse(popup.visible)
                self.assertFalse(popup.winfo_ismapped())
        finally:
            popup.destroy()

    def test_popup_ignores_transient_focus_gap_after_dwm_exposure(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        work_area = Rect(0, 0, 1920, 1040)
        try:
            popup.prepare(config, {}, work_area)
            popup.show(config, {}, Point(20, 20), work_area)
            self.root.update()
            self.assertTrue(popup.visible)
            self.assertIs(popup.focus_get(), popup._search_entry._entry)

            # Reproduce the full-suite race: a delayed DWM FocusOut arrives
            # after arming has completed, while Tk briefly reports no focus.
            with patch.object(popup, "focus_get", return_value=None):
                popup._on_focus_out(tk.Event())
                self.root.update_idletasks()
                self.assertTrue(popup.visible)

            self.root.after(80, self.root.quit)
            self.root.mainloop()
            self.assertTrue(popup.visible)
            self.assertIs(popup.focus_get(), popup._search_entry._entry)
        finally:
            popup.hide()
            self.root.update()
            popup.destroy()

    def test_popup_still_hides_after_focus_really_moves_outside(self) -> None:
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
            self.assertTrue(popup.visible)

            with patch.object(popup, "focus_get", return_value=None):
                popup._on_focus_out(tk.Event())
                self.root.after(360, self.root.quit)
                self.root.mainloop()

            self.assertFalse(popup.visible)
        finally:
            popup.hide()
            self.root.update()
            popup.destroy()

    def test_runtime_state_update_preserves_visible_focus_and_mapping(self) -> None:
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
            popup.show(
                config,
                {},
                Point(20, 20),
                Rect(0, 0, 1920, 1040),
            )
            self.root.update()
            popup._focus_card(1)
            self.root.update()
            generation = popup._show_generation
            render_count = popup.render_count

            with (
                patch.object(popup, "deiconify", wraps=popup.deiconify) as deiconify,
                patch.object(popup, "lift", wraps=popup.lift) as lift,
                patch.object(popup, "focus_force", wraps=popup.focus_force) as focus_force,
            ):
                applied = popup.apply_runtime_state(
                    config,
                    {config.items[0].id: PathStatus.MISSING},
                )

            self.assertTrue(applied)
            self.assertEqual(generation, popup._show_generation)
            self.assertEqual(render_count, popup.render_count)
            self.assertIs(popup.focus_get(), popup._cards[1]._canvas)
            deiconify.assert_not_called()
            lift.assert_not_called()
            focus_force.assert_not_called()
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_search_is_prewarmed_focused_and_reset_on_reopen(self) -> None:
        config = LauncherConfig.default()
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        work_area = Rect(0, 0, 1920, 1040)
        try:
            popup.prepare(config, {}, work_area)
            search_entry = popup._search_entry
            first_cards = tuple(popup._cards)
            render_count = popup.render_count
            self.assertIsNotNone(search_entry)

            popup.show(config, {}, Point(20, 20), work_area)
            self.root.update()
            assert search_entry is not None
            self.assertIs(popup.focus_get(), search_entry._entry)
            self.assertEqual(first_cards, tuple(popup._visible_cards))
            self.assertEqual(render_count, popup.render_count)

            assert popup._search_variable is not None
            popup._search_variable.set("문서")
            self.root.update()
            self.assertNotEqual(first_cards, tuple(popup._visible_cards))

            popup.hide()
            popup.show(config, {}, Point(20, 20), work_area)
            self.root.update()
            self.assertEqual("", popup._search_variable.get())
            self.assertEqual(first_cards, tuple(popup._visible_cards))
            self.assertIs(search_entry, popup._search_entry)
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(render_count, popup.render_count)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_popup_search_filters_unicode_choseong_without_io_or_rebuild(self) -> None:
        config = LauncherConfig(
            columns=3,
            items=[
                LauncherItem(
                    name="Example User",
                    path=r"C:\Profiles\sample",
                    type="folder",
                    order=0,
                ),
                LauncherItem(
                    name="김민수 검사 기준서",
                    path=r"C:\Quality\standard.pdf",
                    type="file",
                    order=1,
                ),
                LauncherItem(
                    name="주간 자료",
                    path=r"C:\품질문서\생산일지.xlsx",
                    type="file",
                    order=2,
                ),
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
            popup.prepare(config, {}, Rect(0, 0, 1920, 1040))
            first_cards = tuple(popup._cards)
            render_count = popup.render_count
            assert popup._search_variable is not None

            with (
                patch("os.path.exists", side_effect=AssertionError("filesystem access")),
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("network access"),
                ),
            ):
                popup._search_variable.set("ㄱㅁㅅ ㄱㅅㄱㅈㅅ")
                self.assertEqual(
                    [popup._card_by_item_id[config.items[1].id]],
                    popup._visible_cards,
                )
                popup._search_variable.set("ＳＡＭＰＬＥ")
                self.assertEqual(
                    [popup._card_by_item_id[config.items[0].id]],
                    popup._visible_cards,
                )
                popup._search_variable.set("")

            self.assertEqual(first_cards, tuple(popup._visible_cards))
            self.assertEqual(first_cards, tuple(popup._cards))
            self.assertEqual(render_count, popup.render_count)
        finally:
            popup.destroy()

    def test_popup_search_keyboard_actions_and_zero_result_settings(self) -> None:
        config = LauncherConfig.default()
        activated: list[str] = []
        settings_opened: list[bool] = []
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=activated.append,
                relocate=lambda _item: None,
                open_settings=lambda: settings_opened.append(True),
            ),
        )
        work_area = Rect(0, 0, 1920, 1040)
        try:
            popup.show(config, {}, Point(20, 20), work_area)
            self.root.update()
            assert popup._search_variable is not None
            assert popup._search_entry is not None
            self.assertTrue(popup.bind("<Control-f>"))
            self.assertTrue(popup._search_entry._entry.bind("<Down>"))
            self.assertTrue(popup._search_entry._entry.bind("<Return>"))

            popup._search_variable.set("문서")
            popup._focus_search_result(1)
            self.root.update()
            first_result = popup._visible_cards[0]
            self.assertIs(popup.focus_get(), first_result._canvas)
            popup._focus_search()
            self.assertIs(popup.focus_get(), popup._search_entry._entry)
            popup._activate_first_search_result()
            self.assertEqual([config.items[1].id], activated)

            popup.show(config, {}, Point(20, 20), work_area)
            popup._search_variable.set("결과가 절대 없는 검색")
            self.root.update()
            self.assertEqual([], popup._visible_cards)
            self.assertTrue(popup._showing_search_empty)
            self.assertEqual("place", popup._search_empty_state.winfo_manager())
            assert popup._search_empty_button is not None
            popup._search_empty_button.invoke()
            self.assertEqual([True], settings_opened)

            popup.show(config, {}, Point(20, 20), work_area)
            popup._search_variable.set("문서")
            popup._on_escape()
            self.assertTrue(popup.visible)
            self.assertEqual("", popup._search_variable.get())
            self.assertEqual(popup._cards, popup._visible_cards)
            popup._on_escape()
            self.assertFalse(popup.visible)
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_runtime_state_update_preserves_active_search_focus_and_scroll(self) -> None:
        config = LauncherConfig(
            columns=3,
            items=[
                LauncherItem(
                    name=("유지" if index < 18 else "제외") + f" 항목 {index + 1}",
                    path=rf"C:\QuickAccess\Search-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(24)
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
            popup.show(config, {}, Point(20, 20), Rect(0, 0, 640, 340))
            self.root.update()
            assert popup._search_variable is not None
            popup._search_variable.set("유지")
            popup._focus_card(len(popup._visible_cards) - 1)
            self.root.update()
            canvas = popup._items_frame._parent_canvas
            scroll_before = canvas.yview()
            focused_before = popup.focus_get()
            visible_before = tuple(popup._visible_cards)

            with (
                patch.object(popup, "deiconify", wraps=popup.deiconify) as deiconify,
                patch.object(popup, "lift", wraps=popup.lift) as lift,
                patch.object(popup, "geometry", wraps=popup.geometry) as geometry,
            ):
                applied = popup.apply_runtime_state(
                    config,
                    {config.items[0].id: PathStatus.TIMEOUT},
                )

            self.assertTrue(applied)
            self.assertEqual("유지", popup._search_variable.get())
            self.assertEqual(visible_before, tuple(popup._visible_cards))
            self.assertIs(focused_before, popup.focus_get())
            self.assertEqual(scroll_before, canvas.yview())
            deiconify.assert_not_called()
            lift.assert_not_called()
            geometry.assert_not_called()
        finally:
            popup.hide()
            self.root.after(50, self.root.quit)
            self.root.mainloop()
            popup.destroy()

    def test_timeout_status_keeps_an_icon_that_arrived_before_validation(self) -> None:
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
            pixels = bytes([20, 80, 160, 255] * (24 * 24))
            pil_image = Image.frombuffer("RGBA", (24, 24), pixels, "raw", "BGRA", 0, 1)
            icon = ctk.CTkImage(
                light_image=pil_image,
                dark_image=pil_image,
                size=(24, 24),
            )
            item = config.items[0]
            key = icon_key(item.path, item.type)
            work_area = Rect(0, 0, 1920, 1040)
            popup.prepare(config, {}, work_area)

            self.assertTrue(popup.apply_runtime_state(config, {}, {key: icon}))
            card = popup._card_by_item_id[item.id]
            self.assertIs(icon, card._icon_label.cget("image"))

            self.assertTrue(
                popup.apply_runtime_state(
                    config,
                    {item.id: PathStatus.TIMEOUT},
                    {key: icon},
                )
            )
            self.assertIs(icon, card._icon_label.cget("image"))
            self.assertFalse(card._broken)
            self.assertTrue(card._timed_out)
            self.assertEqual("warning", card._style_state)
        finally:
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
                first_search_entry = popup._search_entry
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
                self.assertEqual("grid", popup._search_entry.winfo_manager())
                self.assertEqual(0, int(popup._search_entry.grid_info()["column"]))
                self.assertEqual(4, int(popup._search_entry.grid_info()["columnspan"]))
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
            self.assertIs(first_search_entry, popup._search_entry)
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

    def test_prepared_monitor_popups_open_at_target_dpi_without_delayed_reshow(
        self,
    ) -> None:
        config = LauncherConfig(
            columns=3,
            items=[
                LauncherItem(
                    name=f"혼합 DPI {index + 1}",
                    path=rf"C:\QuickAccess\Mixed-{index + 1}",
                    type="file",
                    order=index,
                )
                for index in range(12)
            ],
        )
        actions = PopupActions(
            activate=lambda _item: None,
            relocate=lambda _item: None,
            open_settings=lambda: None,
        )
        work_area = Rect(0, 0, 900, 700)
        popup_100 = PopupPanel(self.root, actions)
        popup_200 = PopupPanel(self.root, actions)
        try:
            popup_100.prepare(
                config,
                {},
                work_area,
                target_dpi_scale=1.0,
            )
            popup_200.prepare(
                config,
                {},
                work_area,
                target_dpi_scale=2.0,
            )
            self.assertEqual(3, popup_100._layout_columns)
            self.assertFalse(popup_100._scrolling)
            self.assertEqual(2, popup_200._layout_columns)
            self.assertTrue(popup_200._scrolling)
            first_trees = {
                popup_100: (
                    popup_100.winfo_children()[0],
                    tuple(popup_100._cards),
                    popup_100.render_count,
                ),
                popup_200: (
                    popup_200.winfo_children()[0],
                    tuple(popup_200._cards),
                    popup_200.render_count,
                ),
            }

            for popup, scale in ((popup_100, 1.0), (popup_200, 2.0)):
                generation = popup._show_generation
                with patch.object(popup, "after", wraps=popup.after) as schedule:
                    popup.show(
                        config,
                        {},
                        Point(890, 690),
                        work_area,
                        target_dpi_scale=scale,
                    )
                self.assertEqual(generation + 1, popup._show_generation)
                self.assertFalse(
                    any(call.args and call.args[0] == 80 for call in schedule.call_args_list)
                )
                popup.hide()

            for popup, (shell, cards, render_count) in first_trees.items():
                self.assertIs(shell, popup.winfo_children()[0])
                self.assertEqual(cards, tuple(popup._cards))
                self.assertEqual(render_count, popup.render_count)
        finally:
            popup_100.destroy()
            popup_200.destroy()

    def test_inactive_monitor_popup_is_refreshed_before_its_next_show(self) -> None:
        config = LauncherConfig.default()
        actions = PopupActions(
            activate=lambda _item: None,
            relocate=lambda _item: None,
            open_settings=lambda: None,
        )
        work_area = Rect(0, 0, 1920, 1040)
        context_100 = MonitorContext("DISPLAY-A", work_area, work_area, 1.0)
        context_200 = MonitorContext("DISPLAY-B", work_area, work_area, 2.0)
        popup_100 = PopupPanel(self.root, actions)
        popup_200 = PopupPanel(self.root, actions)
        try:
            popup_100.prepare(
                config,
                {},
                work_area,
                target_dpi_scale=1.0,
            )
            popup_200.prepare(
                config,
                {},
                work_area,
                target_dpi_scale=2.0,
            )
            previous_render_count = popup_200.render_count
            config.add_item(r"C:\QuickAccess\New", name="새 항목")
            harness = type(
                "InactivePoolHarness",
                (),
                {
                    "_popup_pool": {
                        context_100.cache_key: popup_100,
                        context_200.cache_key: popup_200,
                    },
                    "_popup_contexts": {
                        context_100.cache_key: context_100,
                        context_200.cache_key: context_200,
                    },
                    "config": config,
                    "statuses": {},
                    "icon_images": {},
                },
            )()

            QuickAccessApp._prepare_inactive_popups(harness, popup_100)  # type: ignore[arg-type]

            self.assertEqual(previous_render_count + 1, popup_200.render_count)
            refreshed_render_count = popup_200.render_count
            popup_200.show(
                config,
                {},
                Point(100, 100),
                work_area,
                target_dpi_scale=2.0,
            )
            self.assertEqual(refreshed_render_count, popup_200.render_count)
        finally:
            popup_100.destroy()
            popup_200.destroy()

    def test_stale_target_dpi_is_corrected_without_recursive_show(self) -> None:
        config = LauncherConfig.default()
        work_area = Rect(0, 0, 1920, 1040)
        popup = PopupPanel(
            self.root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )
        try:
            popup.prepare(
                config,
                {},
                work_area,
                target_dpi_scale=1.0,
            )
            with patch.object(popup, "show", wraps=popup.show) as show:
                popup.show(
                    config,
                    {},
                    Point(100, 100),
                    work_area,
                    target_dpi_scale=1.0,
                )
                generation = popup._show_generation
                with patch(
                    "quickaccess.ui.popup.ScalingTracker.get_window_dpi_scaling",
                    return_value=2.0,
                ):
                    popup._validate_mapped_dpi(
                        generation,
                        1.0,
                        config,
                        {},
                        Point(100, 100),
                        work_area,
                        {},
                    )

            self.assertEqual(1, show.call_count)
            self.assertEqual(generation, popup._show_generation)
            self.assertEqual(2.0, popup._prepared_dpi_scale)
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
