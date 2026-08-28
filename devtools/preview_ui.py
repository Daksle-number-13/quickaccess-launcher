"""Render the QuickAccess UI with inert sample data for visual review."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageGrab


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quickaccess.models import LauncherConfig, LauncherItem  # noqa: E402
from quickaccess.platform import enable_dpi_awareness  # noqa: E402
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
            LauncherItem(name="사내 포털", path="https://example.com/portal", type="url", order=4),
        ],
    )


def capture(widget: ctk.CTkBaseClass, output: Path, root: ctk.CTk) -> None:
    widget.update_idletasks()
    widget.lift()
    widget.update()
    hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT
    bounds = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
        raise ctypes.WinError()
    # Exclude the invisible DWM resize shadow from documentation captures.
    # It otherwise records whatever application happens to sit behind the
    # preview as dark strips around an otherwise correct window.
    try:
        visible_bounds = wintypes.RECT()
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            9,  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(visible_bounds),
            ctypes.sizeof(visible_bounds),
        )
        if result == 0:
            bounds = visible_bounds
    except (AttributeError, OSError):
        pass
    output.parent.mkdir(parents=True, exist_ok=True)
    # Crop from one virtual-desktop capture.  Passing an absolute multi-monitor
    # bbox directly to ImageGrab can offset the region when a monitor begins at
    # a negative coordinate; direct HWND capture can omit Tk canvas children.
    virtual_left = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    virtual_top = ctypes.windll.user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    desktop = ImageGrab.grab(all_screens=True)
    captured = desktop.crop(
        (
            bounds.left - virtual_left,
            bounds.top - virtual_top,
            bounds.right - virtual_left,
            bounds.bottom - virtual_top,
        )
    )
    # Rounded Windows corners are outside the app surface.  Composite only
    # that outside area onto white so the screenshot remains an actual UI
    # capture without leaking pixels from another desktop application.
    radius = 24 if isinstance(widget, PopupPanel) else 8
    mask = Image.new("L", captured.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, captured.width - 1, captured.height - 1),
        radius=radius,
        fill=255,
    )
    clean_capture = Image.new("RGB", captured.size, "white")
    clean_capture.paste(captured, mask=mask)
    clean_capture.save(output)
    root.quit()


def render(mode: str, appearance: str, output: Path, page: str) -> None:
    enable_dpi_awareness()
    ctk.set_appearance_mode(appearance)
    root = ctk.CTk()
    root.withdraw()
    # ``CTk.mainloop`` performs a temporary title-bar update before entering
    # Tk's real event loop.  That nested update can consume our delayed capture
    # and then enter the real loop after ``quit`` was sent.  The documentation
    # root stays withdrawn, so skipping that one-time decoration pass is safe.
    root._window_exists = True
    config = sample_config()
    config.set_appearance_mode(appearance)

    if mode == "popup":
        widget: PopupPanel | SettingsWindow = PopupPanel(
            root,
            PopupActions(activate=lambda _item: None, relocate=lambda _item: None, open_settings=lambda: None),
        )
        widget.show(
            config,
            {config.items[3].id: PathStatus.MISSING},
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
                set_update_checks=lambda _enabled: True,
                set_hotkeys=lambda _panel, _quick: True,
            ),
        )
        widget.show()
        widget._select_page(page)

    # Preview capture must stay above whichever desktop application currently
    # owns foreground focus.  Production windows keep their normal policy.
    widget.attributes("-topmost", True)
    if mode == "popup":
        # A documentation capture must not disappear if the screenshot tool
        # briefly takes foreground focus from the production-style flyout.
        widget.unbind("<FocusOut>")
        widget.geometry("+80+80")

    root.after(2500, lambda: capture(widget, output, root))
    root.mainloop()
    root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("popup", "settings"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--appearance", choices=("light", "dark"), default="dark")
    parser.add_argument(
        "--page",
        choices=("items", "shortcuts", "appearance", "about", "preferences"),
        default="items",
    )
    args = parser.parse_args()
    render(args.mode, args.appearance, args.output, args.page)


if __name__ == "__main__":
    main()
