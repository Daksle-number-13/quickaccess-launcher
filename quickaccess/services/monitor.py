"""Windows cursor and monitor-work-area helpers.

The geometry calculation is deliberately kept pure so it can be tested without a
GUI or a Windows desktop.  Coordinates are allowed to be negative because that
is normal for monitors positioned left of or above the primary display.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


class MonitorUnavailableError(RuntimeError):
    """Raised when native monitor information cannot be queried."""


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError("window dimensions must not be negative")


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("invalid rectangle")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def clamp_window_to_work_area(cursor: Point, window: Size, work_area: Rect) -> Point:
    """Clamp a cursor-anchored window's top-left corner to a monitor work area.

    If the requested window is larger than the work area, its corresponding
    coordinate is anchored at the work area's top/left.  The caller can then
    constrain or scroll the window contents.
    """

    max_x = max(work_area.left, work_area.right - window.width)
    max_y = max(work_area.top, work_area.bottom - window.height)
    x = min(max(cursor.x, work_area.left), max_x)
    y = min(max(cursor.y, work_area.top), max_y)
    return Point(x, y)


def center_window_in_work_area(window: Size, work_area: Rect) -> Point:
    """Return a centered position, clamped when the window is oversized."""

    x = work_area.left + max(0, (work_area.width - window.width) // 2)
    y = work_area.top + max(0, (work_area.height - window.height) // 2)
    return clamp_window_to_work_area(Point(x, y), window, work_area)


class _NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _NativeRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _NativeRect),
        ("rcWork", _NativeRect),
        ("dwFlags", ctypes.c_ulong),
    ]


class NativeMonitorService:
    """Small ctypes wrapper around cursor and monitor APIs."""

    MONITOR_DEFAULTTONEAREST = 2

    def __init__(self, user32: object | None = None) -> None:
        self._user32 = user32

    def _api(self) -> object:
        if self._user32 is not None:
            return self._user32
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise MonitorUnavailableError("monitor APIs are available only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(_NativePoint)]
        self._user32.GetCursorPos.restype = ctypes.c_bool
        self._user32.MonitorFromPoint.argtypes = [_NativePoint, ctypes.c_ulong]
        self._user32.MonitorFromPoint.restype = ctypes.c_void_p
        self._user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_MonitorInfo),
        ]
        self._user32.GetMonitorInfoW.restype = ctypes.c_bool
        return self._user32

    def get_cursor_position(self) -> Point:
        api = self._api()
        native_point = _NativePoint()
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        if not api.GetCursorPos(ctypes.byref(native_point)):
            self._raise_last_error("GetCursorPos")
        return Point(int(native_point.x), int(native_point.y))

    def get_monitor_work_area(self, point: Point) -> Rect:
        api = self._api()
        native_point = _NativePoint(point.x, point.y)
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        monitor = api.MonitorFromPoint(native_point, self.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            self._raise_last_error("MonitorFromPoint")

        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        if not api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            self._raise_last_error("GetMonitorInfoW")
        work = info.rcWork
        return Rect(int(work.left), int(work.top), int(work.right), int(work.bottom))

    def popup_position(
        self,
        window: Size,
        cursor: Point | None = None,
    ) -> Point:
        anchor = cursor if cursor is not None else self.get_cursor_position()
        work_area = self.get_monitor_work_area(anchor)
        return clamp_window_to_work_area(anchor, window, work_area)

    @staticmethod
    def _raise_last_error(api_name: str) -> None:
        error_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        if sys.platform == "win32" and hasattr(ctypes, "WinError"):
            error = ctypes.WinError(error_code)
            raise MonitorUnavailableError(f"{api_name} failed: {error}") from error
        raise MonitorUnavailableError(f"{api_name} failed with error {error_code}")


__all__ = [
    "MonitorUnavailableError",
    "NativeMonitorService",
    "Point",
    "Rect",
    "Size",
    "center_window_in_work_area",
    "clamp_window_to_work_area",
]
