"""Render the QuickAccess UI with inert sample data for visual review."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys

import customtkinter as ctk
from PIL import ImageGrab


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quickaccess.models import LauncherConfig, LauncherItem  # noqa: E402
from quickaccess.services.monitor import Point, Rect  # noqa: E402
from quickaccess.services.validation import PathStatus  # noqa: E402
from quickaccess.ui.popup import PopupActions, PopupPanel  # noqa: E402
from quickaccess.ui.settings import SettingsActions, SettingsWindow  # noqa: E402


def sample_config() -> LauncherConfig:
    return LauncherConfig(
        columns=3,
        items=[
            LauncherItem(name="품질 문서", path=r"C:\Quality\Documents", type="folder", order=0),
            LauncherItem(name="불량 집계", path=r"C:\Quality\Reports\불량집계.xlsx", type="file", order=1),
            LauncherItem(name="주간 회의록", path=r"C:\Quality\Meetings\2026", type="folder", order=2),
            LauncherItem(name="검사 기준서", path=r"Z:\Shared\검사 기준서.pdf", type="file", order=3),
        ],
    )


def capture(widget: ctk.CTkBaseClass, output: Path, root: ctk.CTk) -> None:
    widget.update_idletasks()
    hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT
    bounds = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
        raise ctypes.WinError()
    output.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(
        bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom),
        all_screens=True,
    ).save(output)
    root.quit()


def render(mode: str, appearance: str, output: Path, page: str) -> None:
    ctk.set_appearance_mode(appearance)
    root = ctk.CTk()
    root.withdraw()
    config = sample_config()
    config.set_appearance_mode(appearance)

    if mode == "popup":
        widget: PopupPanel | SettingsWindow = PopupPanel(
            root,
            PopupActions(activate=lambda _item: None, relocate=lambda _item: None, open_settings=lambda: None),
        )
        widget.show(
            config,
            {config.items[-1].id: PathStatus.MISSING},
            Point(80, 80),
            Rect(0, 0, 1920, 1040),
        )
    else:
        widget = SettingsWindow(
            root,
            SettingsActions(
                get_config=lambda: deepcopy(config),
                add_item=lambda *args, **kwargs: True,
                delete_item=lambda _item: True,
                rename_item=lambda _item, _name: True,
                move_item=lambda _item, _index: True,
                set_appearance_mode=lambda _mode: True,
                set_columns=lambda _columns: True,
                set_startup=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )
        widget.show()
        widget._select_page(page)

    # Preview capture must stay above whichever desktop application currently
    # owns foreground focus.  Production windows keep their normal policy.
    widget.attributes("-topmost", True)
    if mode == "popup":
        widget.geometry("+80+80")

    root.after(2500, lambda: capture(widget, output, root))
    root.mainloop()
    root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("popup", "settings"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--appearance", choices=("light", "dark"), default="dark")
    parser.add_argument("--page", choices=("items", "preferences"), default="items")
    args = parser.parse_args()
    render(args.mode, args.appearance, args.output, args.page)


if __name__ == "__main__":
    main()
