"""Single-instance guard backed by a Windows Local named mutex."""

from __future__ import annotations

import ctypes
import sys
import threading


ERROR_ALREADY_EXISTS = 183


class SingletonUnavailableError(RuntimeError):
    """Raised when a native mutex operation fails."""


def local_mutex_name(name: str) -> str:
    """Return a validated session-local Windows mutex name."""

    if not name or "\x00" in name:
        raise ValueError("mutex name must be non-empty and must not contain NUL")
    if name.startswith("Local\\"):
        suffix = name[len("Local\\") :]
    else:
        suffix = name
    if not suffix or "\\" in suffix:
        raise ValueError("mutex name must not contain namespace separators")
    return f"Local\\{suffix}"


class SingleInstanceGuard:
    """Keep a mutex handle alive for the lifetime of the application.

    On a non-Windows host ``acquire`` intentionally fails open so importing and
    exercising the rest of the application remains possible.  ``supported``
    lets callers report that no native enforcement is active.
    """

    def __init__(self, name: str = "QuickAccessLauncher", kernel32: object | None = None) -> None:
        self.name = local_mutex_name(name)
        self._kernel32 = kernel32
        self._handle: object | None = None
        self._attempted = False
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return self._kernel32 is not None or (
            sys.platform == "win32" and hasattr(ctypes, "WinDLL")
        )

    @property
    def acquired(self) -> bool:
        return self._handle is not None or (self._attempted and not self.supported)

    @property
    def already_running(self) -> bool:
        return self._attempted and self._handle is None and self.supported

    def _api(self) -> object:
        if self._kernel32 is not None:
            return self._kernel32
        if not self.supported:
            raise SingletonUnavailableError("named mutexes are available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        return self._kernel32

    def acquire(self) -> bool:
        with self._lock:
            if self._handle is not None:
                return True
            if self._attempted:
                return not self.already_running
            self._attempted = True

            if not self.supported:
                return True

            api = self._api()
            ctypes.set_last_error(0)
            handle = api.CreateMutexW(None, False, self.name)
            if not handle:
                self._attempted = False
                self._raise_last_error("CreateMutexW")

            error_code = ctypes.get_last_error()
            if error_code == ERROR_ALREADY_EXISTS:
                api.CloseHandle(handle)
                return False
            self._handle = handle
            return True

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            self._attempted = False
            if handle is not None:
                api = self._api()
                if not api.CloseHandle(handle):
                    self._raise_last_error("CloseHandle")

    release = close

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise SingletonUnavailableError("another QuickAccess instance is already running")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _raise_last_error(api_name: str) -> None:
        error_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        if sys.platform == "win32" and hasattr(ctypes, "WinError"):
            error = ctypes.WinError(error_code)
            raise SingletonUnavailableError(f"{api_name} failed: {error}") from error
        raise SingletonUnavailableError(f"{api_name} failed with error {error_code}")


__all__ = [
    "ERROR_ALREADY_EXISTS",
    "SingleInstanceGuard",
    "SingletonUnavailableError",
    "local_mutex_name",
]
