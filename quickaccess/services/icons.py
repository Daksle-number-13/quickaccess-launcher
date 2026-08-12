"""Best-effort Windows shell icon extraction for launcher cards.

Icons are looked up per file extension (folders share a single key) rather
than per item, extracted off the Tk thread, and cached in memory for the
life of the process.  Every failure mode -- pywin32 missing, a locked or
unusual file, a GDI error -- collapses to "no icon available" so the
caller's existing glyph icon keeps rendering.  This module must never
raise into caller code and must never block the Tk thread.
"""

from __future__ import annotations

import ntpath
import threading
from collections.abc import Callable
from dataclasses import dataclass


# Raw SHGetFileInfo flag values (winuser.h).  Hard-coded instead of read off
# the win32gui module so a missing/renamed constant cannot break extraction.
_SHGFI_ICON = 0x000000100
_SHGFI_SMALLICON = 0x000000001


def _load_win32_dependencies() -> tuple[object, object] | None:
    try:
        import win32gui  # type: ignore[import-not-found]
        import win32ui  # type: ignore[import-not-found]
    except ImportError:
        return None
    return win32gui, win32ui


@dataclass(frozen=True, slots=True)
class IconImage:
    """A decoded top-down 32-bpp BGRA icon bitmap.

    Carrying raw bytes instead of a Tk/PIL image keeps this module free of
    any Tk dependency, since Tk image objects may only be constructed on
    the Tk thread that owns the interpreter.
    """

    width: int
    height: int
    bgra: bytes


def icon_key(path: str, item_type: str) -> str:
    """Return the cache key icons are shared under.

    Folders share one icon regardless of name; files share one icon per
    extension, mirroring how Windows Explorer itself groups file-type
    icons.
    """

    if item_type == "folder":
        return "\0folder"
    extension = ntpath.splitext(path)[1].strip().lower()
    return extension or "\0file"


class IconService:
    """Cache Windows shell icons and extract missing ones off the Tk thread."""

    def __init__(
        self,
        on_ready: Callable[[str, IconImage], object] | None = None,
        *,
        size: int = 32,
        on_callback_error: Callable[[Exception], object] | None = None,
        dependencies: tuple[object, object] | None | object = "auto",
    ) -> None:
        self._default_callback = on_ready
        self._size = size
        self._on_callback_error = on_callback_error
        self._lock = threading.Lock()
        self._cache: dict[str, IconImage | None] = {}
        self._pending: set[str] = set()
        self._dependencies = (
            _load_win32_dependencies() if dependencies == "auto" else dependencies
        )

    @property
    def available(self) -> bool:
        return self._dependencies is not None

    def get_cached(self, key: str) -> IconImage | None:
        with self._lock:
            return self._cache.get(key)

    def request(
        self,
        key: str,
        path: str,
        callback: Callable[[str, IconImage], object] | None = None,
    ) -> None:
        """Ensure ``key`` is cached, extracting it on a background thread.

        A key already cached or already being extracted is a silent no-op,
        so calling this once per rendered item is always safe and cheap.
        """

        if not self.available or not path:
            return
        with self._lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)

        resolved_callback = callback or self._default_callback

        def worker() -> None:
            image = self._extract(path)
            with self._lock:
                self._cache[key] = image
                self._pending.discard(key)
            if image is not None and resolved_callback is not None:
                try:
                    resolved_callback(key, image)
                except Exception as error:
                    if self._on_callback_error is not None:
                        try:
                            self._on_callback_error(error)
                        except Exception:
                            pass

        threading.Thread(
            target=worker,
            name=f"QuickAccessIcon-{key.strip(chr(0))}",
            daemon=True,
        ).start()

    def _extract(self, path: str) -> IconImage | None:
        dependencies = self._dependencies
        if dependencies is None:
            return None
        win32gui, win32ui = dependencies
        hicon = None
        try:
            info = win32gui.SHGetFileInfo(
                path, 0, _SHGFI_ICON | _SHGFI_SMALLICON
            )
            hicon = info[0]
            if not hicon:
                return None
            size = self._size
            screen_dc = win32gui.GetDC(0)
            try:
                source_dc = win32ui.CreateDCFromHandle(screen_dc)
                compatible_dc = source_dc.CreateCompatibleDC()
                bitmap = win32ui.CreateBitmap()
                try:
                    bitmap.CreateCompatibleBitmap(source_dc, size, size)
                    compatible_dc.SelectObject(bitmap)
                    compatible_dc.DrawIcon((0, 0), hicon)
                    bitmap_info = bitmap.GetInfo()
                    raw_bits = bitmap.GetBitmapBits(True)
                    return IconImage(
                        width=int(bitmap_info["bmWidth"]),
                        height=int(bitmap_info["bmHeight"]),
                        bgra=bytes(raw_bits),
                    )
                finally:
                    win32gui.DeleteObject(bitmap.GetHandle())
                    compatible_dc.DeleteDC()
                    source_dc.DeleteDC()
            finally:
                win32gui.ReleaseDC(0, screen_dc)
        except Exception:
            return None
        finally:
            if hicon:
                try:
                    win32gui.DestroyIcon(hicon)
                except Exception:
                    pass


__all__ = ["IconImage", "IconService", "icon_key"]
