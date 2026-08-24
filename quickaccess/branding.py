"""Shared QuickAccess brand artwork used throughout the application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sys

from PIL import Image


def _asset_path() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "assets" / "quickaccess-mark.png"


@lru_cache(maxsize=1)
def _source_image() -> Image.Image:
    with Image.open(_asset_path()) as source:
        return source.convert("RGBA")


@lru_cache(maxsize=16)
def create_brand_icon(size: int = 64) -> Image.Image:
    """Return the QuickAccess brand mark at the requested pixel size."""

    if size < 16:
        raise ValueError("brand icon size must be at least 16 pixels")
    return _source_image().resize((size, size), Image.Resampling.LANCZOS)


__all__ = ["create_brand_icon"]
