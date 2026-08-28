"""Windows cursor and monitor-work-area helpers.

The geometry calculation is deliberately kept pure so it can be tested without a
GUI or a Windows desktop.  Coordinates are allowed to be negative because that
is normal for monitors positioned left of or above the primary display.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable


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


@dataclass(frozen=True, slots=True)
class MonitorContext:
    """Stable display details needed before a popup is made visible.

    ``identifier`` is the Windows display-device name (for example,
    ``\\\\.\\DISPLAY1``).  It is safer as a long-lived cache key than a raw
    HMONITOR, whose value can be recycled after a display-topology change.
    ``scale`` is the monitor's Windows scale factor (1.0 == 100%).  Scale
    lookup is deliberately optional so a missing legacy API never prevents
    the work area from being used.
    """

    identifier: str
    bounds: Rect
    work_area: Rect
    scale: float | None = None

    @property
    def cache_key(self) -> tuple[str, int | None]:
        scale_key = None if self.scale is None else round(self.scale * 1000)
        return self.identifier, scale_key


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


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _NativeRect),
        ("rcWork", _NativeRect),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class NativeMonitorService:
    """Small ctypes wrapper around cursor and monitor APIs."""

    MONITOR_DEFAULTTONEAREST = 2

    def __init__(
        self,
        user32: object | None = None,
        shcore: object | None = None,
    ) -> None:
        self._user32 = user32
        self._shcore = shcore

    def _api(self) -> object:
        if self._user32 is not None:
            return self._user32
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise MonitorUnavailableError("monitor APIs are available only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._set_signature(
            self._user32.GetCursorPos,
            [ctypes.POINTER(_NativePoint)],
            wintypes.BOOL,
        )
        self._set_signature(
            self._user32.MonitorFromPoint,
            [_NativePoint, ctypes.c_ulong],
            ctypes.c_void_p,
        )
        self._set_signature(
            self._user32.GetMonitorInfoW,
            [ctypes.c_void_p, ctypes.c_void_p],
            wintypes.BOOL,
        )
        enum_monitors = getattr(self._user32, "EnumDisplayMonitors", None)
        if enum_monitors is not None:
            self._set_signature(
                enum_monitors,
                [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t],
                wintypes.BOOL,
            )
        return self._user32

    def _scale_api(self) -> object | None:
        if self._shcore is not None:
            return self._shcore
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            return None
        try:
            self._shcore = ctypes.WinDLL("shcore", use_last_error=True)
            function = self._shcore.GetScaleFactorForMonitor
            self._set_signature(
                function,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
                ctypes.c_long,
            )
        except (AttributeError, OSError):
            return None
        return self._shcore

    def get_cursor_position(self) -> Point:
        api = self._api()
        native_point = _NativePoint()
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        if not api.GetCursorPos(ctypes.byref(native_point)):
            self._raise_last_error("GetCursorPos")
        return Point(int(native_point.x), int(native_point.y))

    def get_monitor_work_area(self, point: Point) -> Rect:
        return self.get_monitor_context(point).work_area

    def get_monitor_context(self, point: Point) -> MonitorContext:
        """Return work area and scale for the monitor nearest ``point``."""

        api = self._api()
        native_point = _NativePoint(point.x, point.y)
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        monitor = api.MonitorFromPoint(native_point, self.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            self._raise_last_error("MonitorFromPoint")

        return self._context_for_handle(monitor)

    def get_monitor_contexts(self) -> tuple[MonitorContext, ...]:
        """Enumerate active displays in deterministic desktop order."""

        api = self._api()
        enum_monitors = getattr(api, "EnumDisplayMonitors", None)
        if enum_monitors is None:
            return (self.get_monitor_context(self.get_cursor_position()),)

        handles: list[object] = []
        callback_factory: Callable[..., object] = getattr(
            ctypes,
            "WINFUNCTYPE",
            ctypes.CFUNCTYPE,
        )
        callback_type = callback_factory(
            wintypes.BOOL,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_NativeRect),
            ctypes.c_ssize_t,
        )

        def collect(
            monitor: object,
            _device_context: object,
            _monitor_rect: object,
            _user_data: object,
        ) -> bool:
            handles.append(monitor)
            return True

        callback = callback_type(collect)
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        if not enum_monitors(None, None, callback, 0):
            self._raise_last_error("EnumDisplayMonitors")
        contexts = [self._context_for_handle(handle) for handle in handles]
        contexts.sort(
            key=lambda context: (
                context.bounds.top,
                context.bounds.left,
                context.identifier,
            )
        )
        return tuple(contexts)

    def _context_for_handle(self, monitor: object) -> MonitorContext:
        api = self._api()

        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        if not api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            self._raise_last_error("GetMonitorInfoW")
        bounds = info.rcMonitor
        work = info.rcWork
        identifier = str(info.szDevice).strip() or self._handle_identifier(monitor)
        return MonitorContext(
            identifier=identifier,
            bounds=Rect(
                int(bounds.left),
                int(bounds.top),
                int(bounds.right),
                int(bounds.bottom),
            ),
            work_area=Rect(
                int(work.left),
                int(work.top),
                int(work.right),
                int(work.bottom),
            ),
            scale=self._get_monitor_scale(monitor),
        )

    def _get_monitor_scale(self, monitor: object) -> float | None:
        """Best-effort scale lookup that cannot invalidate monitor geometry."""

        api = self._scale_api()
        if api is None:
            return None
        function = getattr(api, "GetScaleFactorForMonitor", None)
        if function is None:
            return None
        factor = ctypes.c_int()
        try:
            result = int(function(monitor, ctypes.byref(factor)))
        except Exception:
            return None
        if result != 0 or factor.value <= 0:
            return None
        return factor.value / 100.0

    def popup_position(
        self,
        window: Size,
        cursor: Point | None = None,
    ) -> Point:
        anchor = cursor if cursor is not None else self.get_cursor_position()
        work_area = self.get_monitor_work_area(anchor)
        return clamp_window_to_work_area(anchor, window, work_area)

    @staticmethod
    def _handle_identifier(monitor: object) -> str:
        try:
            value = int(monitor)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = int(getattr(monitor, "value", 0) or 0)
        return f"HMONITOR:{value:x}"

    @staticmethod
    def _set_signature(
        function: object,
        argtypes: list[object],
        restype: object,
    ) -> None:
        try:
            setattr(function, "argtypes", argtypes)
            setattr(function, "restype", restype)
        except (AttributeError, TypeError):
            pass

    @staticmethod
    def _raise_last_error(api_name: str) -> None:
        error_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        if sys.platform == "win32" and hasattr(ctypes, "WinError"):
            error = ctypes.WinError(error_code)
            raise MonitorUnavailableError(f"{api_name} failed: {error}") from error
        raise MonitorUnavailableError(f"{api_name} failed with error {error_code}")


__all__ = [
    "MonitorUnavailableError",
    "MonitorContext",
    "NativeMonitorService",
    "Point",
    "Rect",
    "Size",
    "center_window_in_work_area",
    "clamp_window_to_work_area",
]
