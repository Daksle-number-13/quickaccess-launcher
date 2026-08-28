"""Single-instance ownership and best-effort same-session activation signals.

The mutex remains the authority for deciding which process owns QuickAccess.
Small named auto-reset events let a later process ask that owner to show its
panel or settings. They use the Windows ``Local`` kernel-object namespace,
need no network, file, registry, administrator, or background-thread access,
and are polled without blocking by the UI process.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from enum import Enum
from typing import Protocol


ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class SingletonUnavailableError(RuntimeError):
    """Raised when the authoritative native mutex operation fails."""


class InstanceRequest(str, Enum):
    """Actions a later QuickAccess process can request from the owner."""

    SHOW_PANEL = "show-panel"
    OPEN_SETTINGS = "open-settings"


_SIGNAL_SUFFIX = {
    InstanceRequest.SHOW_PANEL: "ShowPanel",
    InstanceRequest.OPEN_SETTINGS: "OpenSettings",
}


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


def local_signal_name(name: str, request: InstanceRequest | str) -> str:
    """Return the stable local event name used for ``request``."""

    mutex_name = local_mutex_name(name)
    normalized_request = InstanceRequest(request)
    return f"{mutex_name}-Signal-{_SIGNAL_SUFFIX[normalized_request]}"


class _SingletonNativeApi(Protocol):
    """Minimal native surface, deliberately injectable for deterministic tests."""

    @property
    def supported(self) -> bool: ...

    def create_mutex(self, name: str) -> tuple[object | None, int]: ...

    def create_event(self, name: str) -> object | None: ...

    def open_event(self, name: str) -> object | None: ...

    def set_event(self, handle: object) -> bool: ...

    def poll_event(self, handle: object) -> bool: ...

    def close_handle(self, handle: object) -> bool: ...

    def last_error(self) -> int: ...


class _WindowsSingletonApi:
    """Thin adapter around the kernel calls used by :class:`SingleInstanceGuard`."""

    def __init__(self, kernel32: object | None = None) -> None:
        self._kernel32 = kernel32

    @property
    def supported(self) -> bool:
        return self._kernel32 is not None or (
            sys.platform == "win32" and hasattr(ctypes, "WinDLL")
        )

    def _api(self) -> object:
        if self._kernel32 is not None:
            return self._kernel32
        if not self.supported:
            raise SingletonUnavailableError(
                "named mutexes and events are available only on Windows"
            )

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure(self._kernel32)
        return self._kernel32

    @staticmethod
    def _configure(api: object) -> None:
        api.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        api.CreateMutexW.restype = ctypes.c_void_p
        api.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        api.CreateEventW.restype = ctypes.c_void_p
        api.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        api.OpenEventW.restype = ctypes.c_void_p
        api.SetEvent.argtypes = [ctypes.c_void_p]
        api.SetEvent.restype = ctypes.c_bool
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_bool

    @staticmethod
    def _clear_last_error() -> None:
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)

    def create_mutex(self, name: str) -> tuple[object | None, int]:
        self._clear_last_error()
        handle = self._api().CreateMutexW(None, False, name)
        return handle, self.last_error()

    def create_event(self, name: str) -> object | None:
        self._clear_last_error()
        # Auto-reset events naturally coalesce repeated activation requests and
        # let a zero-timeout wait consume one without a ResetEvent race.
        return self._api().CreateEventW(None, False, False, name)

    def open_event(self, name: str) -> object | None:
        self._clear_last_error()
        return self._api().OpenEventW(EVENT_MODIFY_STATE, False, name)

    def set_event(self, handle: object) -> bool:
        self._clear_last_error()
        return bool(self._api().SetEvent(handle))

    def poll_event(self, handle: object) -> bool:
        self._clear_last_error()
        result = int(self._api().WaitForSingleObject(handle, 0))
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise SingletonUnavailableError(
            f"WaitForSingleObject failed with error {self.last_error()}"
        )

    def close_handle(self, handle: object) -> bool:
        self._clear_last_error()
        return bool(self._api().CloseHandle(handle))

    @staticmethod
    def last_error() -> int:
        return ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0


class SingleInstanceGuard:
    """Keep the owner mutex and its optional activation events alive.

    ``acquire`` deliberately fails open on unsupported hosts so importing and
    testing the rest of the application remains possible. Native signaling is
    best effort: inability to create or contact activation events never weakens
    mutex ownership and never prevents the application from starting/exiting.

    The owner should call :meth:`drain_requests` from its existing UI poll. A
    later process calls :meth:`notify_existing` after ``acquire`` returns
    ``False`` and can then exit normally.
    """

    def __init__(
        self,
        name: str = "QuickAccessLauncher",
        kernel32: object | None = None,
        *,
        native_api: _SingletonNativeApi | None = None,
    ) -> None:
        if kernel32 is not None and native_api is not None:
            raise ValueError("pass either kernel32 or native_api, not both")
        self.name = local_mutex_name(name)
        self._native_api: _SingletonNativeApi = (
            native_api if native_api is not None else _WindowsSingletonApi(kernel32)
        )
        self._handle: object | None = None
        self._signal_handles: dict[InstanceRequest, object] = {}
        self._attempted = False
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return self._native_api.supported

    @property
    def acquired(self) -> bool:
        return self._handle is not None or (self._attempted and not self.supported)

    @property
    def already_running(self) -> bool:
        return self._attempted and self._handle is None and self.supported

    @property
    def signaling_available(self) -> bool:
        """Whether this owner has every activation endpoint ready."""

        with self._lock:
            return self._handle is not None and len(self._signal_handles) == len(
                InstanceRequest
            )

    def acquire(self) -> bool:
        with self._lock:
            if self._handle is not None:
                return True
            if self._attempted:
                return not self.already_running
            self._attempted = True

            if not self.supported:
                return True

            handle, error_code = self._native_api.create_mutex(self.name)
            if not handle:
                self._attempted = False
                self._raise_native_error("CreateMutexW")

            if error_code == ERROR_ALREADY_EXISTS:
                # CreateMutexW returns a real handle even when the object
                # already exists. It must not be kept by the losing process.
                self._native_api.close_handle(handle)
                return False

            self._handle = handle
            self._prepare_signal_handles()
            return True

    def notify_existing(
        self,
        request: InstanceRequest | str = InstanceRequest.SHOW_PANEL,
    ) -> bool:
        """Ask the detected owner to act, returning whether it accepted a signal.

        This performs only ``OpenEvent``, ``SetEvent`` and ``CloseHandle`` and
        therefore has no unbounded wait. ``False`` is the clean fallback for
        an older owner, unsupported OS, startup race, access denial, or native
        signaling failure.
        """

        normalized_request = InstanceRequest(request)
        with self._lock:
            if not self.already_running:
                return False
            try:
                handle = self._native_api.open_event(
                    local_signal_name(self.name, normalized_request)
                )
                if not handle:
                    return False
                try:
                    return self._native_api.set_event(handle)
                finally:
                    self._native_api.close_handle(handle)
            except Exception:
                # This channel is deliberately best-effort. Unexpected native
                # adapter failures must still degrade to the old safe exit.
                return False

    def drain_requests(self) -> tuple[InstanceRequest, ...]:
        """Consume pending requests without waiting; callable by the UI thread."""

        with self._lock:
            if self._handle is None or not self._signal_handles:
                return ()

            requests: list[InstanceRequest] = []
            for request in InstanceRequest:
                handle = self._signal_handles.get(request)
                if handle is None:
                    continue
                try:
                    if self._native_api.poll_event(handle):
                        requests.append(request)
                except Exception:
                    # Signaling is convenience-only. A damaged endpoint must
                    # never take down the resident launcher or its mutex.
                    continue
            return tuple(requests)

    def close(self) -> None:
        with self._lock:
            signal_handles = tuple(self._signal_handles.values())
            self._signal_handles.clear()
            handle, self._handle = self._handle, None
            self._attempted = False

            # Close activation events before releasing the authoritative
            # mutex, so a new owner never targets endpoints from the old one.
            for signal_handle in signal_handles:
                try:
                    self._native_api.close_handle(signal_handle)
                except Exception:
                    pass

            if handle is not None and not self._native_api.close_handle(handle):
                self._raise_native_error("CloseHandle")

    release = close

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise SingletonUnavailableError(
                "another QuickAccess instance is already running"
            )
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _prepare_signal_handles(self) -> None:
        created: dict[InstanceRequest, object] = {}
        try:
            for request in InstanceRequest:
                handle = self._native_api.create_event(
                    local_signal_name(self.name, request)
                )
                if not handle:
                    raise SingletonUnavailableError("CreateEventW failed")
                created[request] = handle
        except Exception:
            for handle in created.values():
                try:
                    self._native_api.close_handle(handle)
                except Exception:
                    pass
            return
        self._signal_handles = created

    def _raise_native_error(self, api_name: str) -> None:
        error_code = self._native_api.last_error()
        if sys.platform == "win32" and hasattr(ctypes, "WinError"):
            error = ctypes.WinError(error_code)
            raise SingletonUnavailableError(f"{api_name} failed: {error}") from error
        raise SingletonUnavailableError(f"{api_name} failed with error {error_code}")


__all__ = [
    "ERROR_ALREADY_EXISTS",
    "InstanceRequest",
    "SingleInstanceGuard",
    "SingletonUnavailableError",
    "local_mutex_name",
    "local_signal_name",
]
