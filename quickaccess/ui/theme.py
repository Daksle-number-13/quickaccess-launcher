"""Shared visual tokens for the QuickAccess desktop interface.

Every colour is a ``(light, dark)`` CustomTkinter colour tuple so the same
widgets can follow the application's appearance mode without branching.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

import customtkinter as ctk

from quickaccess.branding import create_brand_icon


Color = tuple[str, str]

# Core surfaces and typography.
BG: Color = ("#F8FAFD", "#0F1115")
SURFACE: Color = ("#FFFFFF", "#191C22")
SURFACE_ALT: Color = ("#F4F7FB", "#20242B")
SURFACE_HOVER: Color = ("#EAF0F7", "#292E37")
BORDER: Color = ("#D7DEE8", "#343A45")
TEXT: Color = ("#17202A", "#F4F6F8")
MUTED: Color = ("#667085", "#9CA6B4")

# Semantic colours.
ACCENT: Color = ("#2563EB", "#5B91FF")
ACCENT_HOVER: Color = ("#1D4ED8", "#76A5FF")
ACCENT_SOFT: Color = ("#E9F0FF", "#192B4D")
DANGER: Color = ("#C4322B", "#FF7B72")
DANGER_HOVER: Color = ("#A92520", "#FF958D")
DANGER_SOFT: Color = ("#FFF0EF", "#3B2022")
DANGER_SOFT_HOVER: Color = ("#FFE3E1", "#4A272A")
SUCCESS: Color = ("#178347", "#4CCB7F")
WARNING: Color = ("#A15C00", "#F6B94A")
WARNING_SOFT: Color = ("#FFF6E5", "#392B16")
WARNING_SOFT_HOVER: Color = ("#FFEDCC", "#49371A")

# Reusable geometry tokens.
WINDOW_RADIUS = 16
CARD_RADIUS = 14
CONTROL_RADIUS = 10
BORDER_WIDTH = 1

FONT_FAMILY = "맑은 고딕"
ICON_FONT_FAMILY = "Segoe Fluent Icons"
ICON_FONT_FALLBACK = "Segoe MDL2 Assets"


def font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    """Return a CustomTkinter-compatible text font tuple."""

    return (FONT_FAMILY, size, weight)


@lru_cache(maxsize=1)
def _resolved_icon_font_family() -> str:
    """Prefer Fluent icons and fall back to the Windows 10 MDL2 font.

    The glyphs used by QuickAccess share code points across both fonts.  The
    file check avoids Tk silently substituting an unrelated font on Windows 10.
    """

    windows_dir = os.environ.get("WINDIR")
    if windows_dir and (Path(windows_dir) / "Fonts" / "SegoeIcons.ttf").is_file():
        return ICON_FONT_FAMILY
    return ICON_FONT_FALLBACK


def icon_font(size: int) -> tuple[str, int]:
    """Return the available monochrome Windows icon font at ``size``."""

    return (_resolved_icon_font_family(), size)


def brand_image(size: int) -> ctk.CTkImage:
    """Return the shared QuickAccess brand mark for a CTk widget."""

    artwork = create_brand_icon(max(32, size * 2))
    return ctk.CTkImage(light_image=artwork, dark_image=artwork, size=(size, size))


__all__ = [
    "ACCENT",
    "ACCENT_HOVER",
    "ACCENT_SOFT",
    "BG",
    "BORDER",
    "BORDER_WIDTH",
    "CARD_RADIUS",
    "CONTROL_RADIUS",
    "DANGER",
    "DANGER_HOVER",
    "DANGER_SOFT",
    "DANGER_SOFT_HOVER",
    "FONT_FAMILY",
    "ICON_FONT_FALLBACK",
    "ICON_FONT_FAMILY",
    "MUTED",
    "SUCCESS",
    "SURFACE",
    "SURFACE_ALT",
    "SURFACE_HOVER",
    "TEXT",
    "WARNING",
    "WARNING_SOFT",
    "WARNING_SOFT_HOVER",
    "WINDOW_RADIUS",
    "font",
    "brand_image",
    "icon_font",
]
