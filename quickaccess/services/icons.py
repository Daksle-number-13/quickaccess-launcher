"""Best-effort native Windows shell icon extraction for launcher cards."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


_SHGFI_ICON = 0x00000100
_SHGFI_SMALLICON = 0x00000001
_DI_NORMAL = 0x0003
_DIB_RGB_COLORS = 0
_BI_RGB = 0


@dataclass(frozen=True, slots=True)
class IconImage:
    """A decoded top-down 32-bpp BGRA icon bitmap."""

    width: int
    height: int
    bgra: bytes


class _IconApi(Protocol):
    def extract(self, path: str, size: int) -> IconImage | None: ...


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", wintypes.BYTE),
        ("rgbGreen", wintypes.BYTE),
        ("rgbRed", wintypes.BYTE),
        ("rgbReserved", wintypes.BYTE),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


class _NativeIconApi:
    """Extract HICON pixels using only stable Win32 APIs and ctypes."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows shell icons are only available on Windows")
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.shell32.SHGetFileInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_SHFILEINFOW),
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.shell32.SHGetFileInfoW.restype = ctypes.c_size_t
        self.user32.DrawIconEx.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HICON,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.HBRUSH,
            wintypes.UINT,
        ]
        self.user32.DrawIconEx.restype = wintypes.BOOL
        self.user32.DestroyIcon.argtypes = [wintypes.HICON]
        self.user32.DestroyIcon.restype = wintypes.BOOL
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(_BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.DeleteDC.restype = wintypes.BOOL

    def extract(self, path: str, size: int) -> IconImage | None:
        info = _SHFILEINFOW()
        result = self.shell32.SHGetFileInfoW(
            path,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            _SHGFI_ICON | _SHGFI_SMALLICON,
        )
        if not result or not info.hIcon:
            return None

        memory_dc = None
        bitmap = None
        old_object = None
        try:
            memory_dc = self.gdi32.CreateCompatibleDC(None)
            if not memory_dc:
                return None
            header = _BITMAPINFOHEADER(
                biSize=ctypes.sizeof(_BITMAPINFOHEADER),
                biWidth=size,
                biHeight=-size,
                biPlanes=1,
                biBitCount=32,
                biCompression=_BI_RGB,
                biSizeImage=size * size * 4,
            )
            bitmap_info = _BITMAPINFO(bmiHeader=header)
            bits = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(
                memory_dc,
                ctypes.byref(bitmap_info),
                _DIB_RGB_COLORS,
                ctypes.byref(bits),
                None,
                0,
            )
            if not bitmap or not bits.value:
                return None
            old_object = self.gdi32.SelectObject(memory_dc, bitmap)
            ctypes.memset(bits, 0, size * size * 4)
            if not self.user32.DrawIconEx(
                memory_dc, 0, 0, info.hIcon, size, size, 0, None, _DI_NORMAL
            ):
                return None
            pixels = bytearray(ctypes.string_at(bits, size * size * 4))
            # Some legacy shell icons do not expose an alpha channel.  Keep
            # their visible pixels opaque instead of returning a blank image.
            if not any(pixels[index] for index in range(3, len(pixels), 4)):
                for index in range(0, len(pixels), 4):
                    if pixels[index] or pixels[index + 1] or pixels[index + 2]:
                        pixels[index + 3] = 255
            return IconImage(size, size, bytes(pixels))
        finally:
            if old_object and memory_dc:
                self.gdi32.SelectObject(memory_dc, old_object)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory_dc:
                self.gdi32.DeleteDC(memory_dc)
            self.user32.DestroyIcon(info.hIcon)


def _load_native_api() -> _IconApi | None:
    try:
        return _NativeIconApi()
    except Exception:
        return None


def icon_key(path: str, item_type: str) -> str:
    if item_type == "folder":
        return "\0folder"
    if item_type == "url":
        return "\0url"
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
        api: _IconApi | None | object = "auto",
    ) -> None:
        self._default_callback = on_ready
        self._size = size
        self._on_callback_error = on_callback_error
        self._lock = threading.Lock()
        self._cache: dict[str, IconImage | None] = {}
        self._pending: set[str] = set()
        self._api = _load_native_api() if api == "auto" else api

    @property
    def available(self) -> bool:
        return self._api is not None

    def get_cached(self, key: str) -> IconImage | None:
        with self._lock:
            return self._cache.get(key)

    def request(
        self,
        key: str,
        path: str,
        callback: Callable[[str, IconImage], object] | None = None,
    ) -> None:
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
        try:
            api = self._api
            return None if api is None else api.extract(path, self._size)  # type: ignore[union-attr]
        except Exception:
            return None


__all__ = ["IconImage", "IconService", "icon_key"]
