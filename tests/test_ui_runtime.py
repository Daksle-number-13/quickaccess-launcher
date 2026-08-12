from __future__ import annotations

import sys
import tkinter as tk
import time
import unittest

import customtkinter as ctk
from PIL import Image

from quickaccess.ui.dialogs import ToastManager
from quickaccess.models import LauncherConfig
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
        popup.hide()
        popup.show(
            config,
            {item.id: PathStatus.VALID for item in config.items},
            anchor,
            work_area,
        )
        self.assertIs(first_shell, popup.winfo_children()[0])

        # CustomTkinter scales the existing widget tree after WM_DPICHANGED.
        # A monitor-scale change alone must not rebuild identical cards; only
        # derived structural changes such as columns/scroll height should.
        original_get_scaling = popup._get_window_scaling
        try:
            popup._get_window_scaling = lambda: 1.5
            popup.prepare(config, {}, work_area)
        finally:
            popup._get_window_scaling = original_get_scaling
        self.assertIs(first_shell, popup.winfo_children()[0])

        popup.show(
            config,
            {config.items[0].id: PathStatus.MISSING},
            anchor,
            work_area,
        )
        self.assertIsNot(first_shell, popup.winfo_children()[0])
        popup.hide()
        # Let the short focus/DPI settle callbacks observe the hidden state
        # before destroying this Toplevel.  This keeps callbacks from one
        # Tcl interpreter from leaking into the next runtime smoke test.
        self.root.after(300, self.root.quit)
        self.root.mainloop()
        popup.destroy()

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
            popup._navigate(0, 0, "right")
            self.root.update()
            self.assertIs(popup.focus_get(), popup._cards[1]._canvas)
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

            popup.show(config, {}, Point(20, 20), Rect(0, 0, 1920, 1040), icons={key: icon})
            self.root.update()

            self.assertIs(popup._cards[0]._icon_label.cget("image"), icon)
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
