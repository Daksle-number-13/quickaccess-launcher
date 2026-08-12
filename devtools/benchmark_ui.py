"""Measure source-mode QuickAccess UI construction and repeat-show latency."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from time import perf_counter

import customtkinter as ctk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quickaccess.models import LauncherConfig, LauncherItem  # noqa: E402
from quickaccess.platform import enable_dpi_awareness  # noqa: E402
from quickaccess.services.monitor import Point, Rect  # noqa: E402
from quickaccess.ui.popup import PopupActions, PopupPanel  # noqa: E402
from quickaccess.ui.settings import SettingsActions, SettingsWindow  # noqa: E402


def sample_config(item_count: int = 20) -> LauncherConfig:
    return LauncherConfig(
        columns=3,
        items=[
            LauncherItem(
                name=f"테스트 항목 {index + 1}",
                path=rf"C:\QuickAccess\Item-{index + 1}",
                type="folder",
                order=index,
            )
            for index in range(item_count)
        ],
    )


def elapsed_ms(callback: object) -> float:
    start = perf_counter()
    callback()  # type: ignore[operator]
    return (perf_counter() - start) * 1000


def main() -> None:
    enable_dpi_awareness()
    ctk.set_appearance_mode("System")
    root = ctk.CTk()
    root.withdraw()
    config = sample_config()
    anchor = Point(100, 100)
    work_area = Rect(0, 0, 1920, 1040)

    popup: PopupPanel | None = None

    def construct_popup() -> None:
        nonlocal popup
        popup = PopupPanel(
            root,
            PopupActions(
                activate=lambda _item: None,
                relocate=lambda _item: None,
                open_settings=lambda: None,
            ),
        )

    timings = {"popup_construct": elapsed_ms(construct_popup)}
    assert popup is not None
    timings["popup_first_show"] = elapsed_ms(
        lambda: (popup.show(config, {}, anchor, work_area), root.update())
    )
    popup.hide()
    root.update()
    timings["popup_repeat_show"] = elapsed_ms(
        lambda: (popup.show(config, {}, anchor, work_area), root.update())
    )
    popup.hide()
    root.update()
    timings["popup_cached_show_call"] = elapsed_ms(
        lambda: popup.show(config, {}, anchor, work_area)
    )
    popup.hide()

    fast_window = ctk.CTkToplevel(root)
    fast_window.withdraw()
    fast_frame = ctk.CTkFrame(fast_window)
    fast_frame.pack(fill="both", expand=True)

    def build_fast_cards() -> None:
        for child in fast_frame.winfo_children():
            child.destroy()
        for index in range(20):
            ctk.CTkButton(
                fast_frame,
                text=f"테스트 항목 {index + 1}\n폴더",
                width=184,
                height=68,
                anchor="w",
            ).grid(row=index // 3, column=index % 3, padx=4, pady=4)
        fast_window.deiconify()
        root.update()

    timings["twenty_ctk_buttons"] = elapsed_ms(build_fast_cards)
    fast_window.withdraw()

    actions = SettingsActions(
        get_config=lambda: deepcopy(config),
        add_item=lambda *args, **kwargs: True,
        delete_item=lambda _item: True,
        rename_item=lambda _item, _name: True,
        move_item=lambda _item, _index: True,
        set_appearance_mode=lambda _mode: True,
        set_columns=lambda _columns: True,
        set_startup=lambda _enabled: True,
        set_hotkeys=lambda _panel, _quick: True,
    )
    settings: SettingsWindow | None = None

    def construct_settings() -> None:
        nonlocal settings
        settings = SettingsWindow(root, actions)

    timings["settings_construct"] = elapsed_ms(construct_settings)
    assert settings is not None
    timings["settings_first_show"] = elapsed_ms(
        lambda: (settings.show(), root.update())
    )
    settings.withdraw()
    root.update()
    timings["settings_repeat_show"] = elapsed_ms(
        lambda: (settings.show(), root.update())
    )
    settings.withdraw()
    root.update()
    settings.refresh = lambda: None  # type: ignore[method-assign]
    timings["settings_cached_show"] = elapsed_ms(
        lambda: (settings.show(), root.update())
    )

    for name, duration in timings.items():
        print(f"{name}={duration:.1f}ms")
    root.destroy()


if __name__ == "__main__":
    main()
