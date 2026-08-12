"""Small Windows platform guards and early DPI-awareness setup."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
import sys


ERROR_ACCESS_DENIED = 5
E_ACCESSDENIED = 0x80070005
PROCESS_PER_MONITOR_DPI_AWARE = 2
# DPI_AWARENESS_CONTEXT values are negative pseudo-handles in WinUser.h.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class UnsupportedPlatformError(RuntimeError):
    """Raised when the Windows-only application is run elsewhere."""


class DpiAwarenessMode(str, Enum):
    PER_MONITOR_V2 = "per_monitor_v2"
    PER_MONITOR = "per_monitor"
    SYSTEM = "system"
    ALREADY_CONFIGURED = "already_configured"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DpiAwarenessResult:
    mode: DpiAwarenessMode
    applied: bool
    error_code: int | None = None


def is_windows() -> bool:
    return sys.platform == "win32"


def require_windows(feature: str = "QuickAccess Launcher") -> None:
    """Fail early with a useful message on unsupported operating systems."""

    if not is_windows():
        raise UnsupportedPlatformError(f"{feature} requires Windows")


def enable_dpi_awareness() -> DpiAwarenessResult:
    """Best-effort DPI setup, to be called before creating any Tk window.

    Per-Monitor-v2 is preferred.  Older APIs are attempted only when the newer
    one is unavailable or fails for a reason other than an already-fixed process
    DPI context.  This function never raises for an OS/API failure.
    """

    if not is_windows():
        return DpiAwarenessResult(DpiAwarenessMode.UNSUPPORTED, applied=False)

    last_error: int | None = None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        return DpiAwarenessResult(DpiAwarenessMode.FAILED, applied=False)

    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        _set_signature(
            set_context,
            [ctypes.c_void_p],
            ctypes.c_bool,
        )
        try:
            ctypes.set_last_error(0)
            context = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            if set_context(context):
                return DpiAwarenessResult(
                    DpiAwarenessMode.PER_MONITOR_V2, applied=True
                )
            last_error = ctypes.get_last_error()
            if last_error == ERROR_ACCESS_DENIED:
                return DpiAwarenessResult(
                    DpiAwarenessMode.ALREADY_CONFIGURED,
                    applied=True,
                    error_code=last_error,
                )
        except Exception:
            last_error = ctypes.get_last_error()

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = shcore.SetProcessDpiAwareness
        _set_signature(set_awareness, [ctypes.c_int], ctypes.c_long)
        result = int(set_awareness(PROCESS_PER_MONITOR_DPI_AWARE))
        if result == 0:
            return DpiAwarenessResult(DpiAwarenessMode.PER_MONITOR, applied=True)
        unsigned_result = result & 0xFFFFFFFF
        if unsigned_result == E_ACCESSDENIED:
            return DpiAwarenessResult(
                DpiAwarenessMode.ALREADY_CONFIGURED,
                applied=True,
                error_code=unsigned_result,
            )
        last_error = unsigned_result
    except Exception:
        pass

    legacy = getattr(user32, "SetProcessDPIAware", None)
    if legacy is not None:
        _set_signature(legacy, [], ctypes.c_bool)
        try:
            ctypes.set_last_error(0)
            if legacy():
                return DpiAwarenessResult(DpiAwarenessMode.SYSTEM, applied=True)
            error = ctypes.get_last_error()
            if error:
                last_error = error
        except Exception:
            pass

    return DpiAwarenessResult(
        DpiAwarenessMode.FAILED,
        applied=False,
        error_code=last_error,
    )


def _set_signature(function: object, argtypes: list[object], restype: object) -> None:
    """Assign ctypes metadata while remaining friendly to test doubles."""

    try:
        setattr(function, "argtypes", argtypes)
        setattr(function, "restype", restype)
    except (AttributeError, TypeError):
        pass
