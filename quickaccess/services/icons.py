"""Best-effort native Windows shell icon extraction for launcher cards."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import ntpath
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
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
    if extension in {".exe", ".ico", ".lnk"}:
        normalized_path = ntpath.normcase(ntpath.normpath(path.strip()))
        return f"\0path:{normalized_path}"
    return extension or "\0file"


IconReadyCallback = Callable[[str, IconImage], object]


@dataclass(slots=True)
class _IconCandidate:
    path: str
    identity: str
    attempt: int = 0


@dataclass(slots=True)
class _KeyRequestState:
    ready: deque[_IconCandidate] = field(default_factory=deque)
    known_paths: set[str] = field(default_factory=set)
    callbacks: list[IconReadyCallback] = field(default_factory=list)
    active: _IconCandidate | None = None
    retry_timers: set[threading.Timer] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _IconWork:
    key: str
    state: _KeyRequestState
    candidate: _IconCandidate


class IconService:
    """Cache Windows shell icons and extract missing ones off the Tk thread."""

    def __init__(
        self,
        on_ready: Callable[[str, IconImage], object] | None = None,
        *,
        size: int = 32,
        on_callback_error: Callable[[Exception], object] | None = None,
        api: _IconApi | None | object = "auto",
        max_workers: int = 4,
        retry_delays: tuple[float, ...] = (0.5, 2.0),
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than zero")
        normalized_retry_delays = tuple(float(delay) for delay in retry_delays)
        if any(
            not math.isfinite(delay) or delay < 0
            for delay in normalized_retry_delays
        ):
            raise ValueError("retry delays must be finite non-negative values")
        self._default_callback = on_ready
        self._size = size
        self._on_callback_error = on_callback_error
        self._retry_delays = normalized_retry_delays
        self._lock = threading.RLock()
        self._callbacks_idle = threading.Condition(self._lock)
        self._cache: dict[str, IconImage] = {}
        self._pending: set[str] = set()
        self._states: dict[str, _KeyRequestState] = {}
        self._closed = False
        self._callbacks_inflight = 0
        self._callback_threads: dict[int, int] = {}
        self._api = _load_native_api() if api == "auto" else api
        self._work_queue: queue.Queue[_IconWork | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        if self._api is not None:
            for index in range(max_workers):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"QuickAccessIconWorker-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

    @property
    def available(self) -> bool:
        with self._lock:
            return self._api is not None and not self._closed

    def get_cached(self, key: str) -> IconImage | None:
        with self._lock:
            return self._cache.get(key)

    def request(
        self,
        key: str,
        path: str,
        callback: IconReadyCallback | None = None,
    ) -> None:
        if self._api is None or not path:
            return
        resolved_callback = callback or self._default_callback
        candidate_identity = ntpath.normcase(ntpath.normpath(path.strip()))
        with self._lock:
            if self._closed or key in self._cache:
                return
            state = self._states.get(key)
            if state is None:
                state = _KeyRequestState()
                self._states[key] = state
                self._pending.add(key)
            self._remember_callback_locked(state, resolved_callback)
            if candidate_identity not in state.known_paths:
                state.known_paths.add(candidate_identity)
                state.ready.append(_IconCandidate(path, candidate_identity))
            self._enqueue_next_locked(key, state)

    def _worker_loop(self) -> None:
        while True:
            work = self._work_queue.get()
            if work is None:
                return
            with self._lock:
                state = self._states.get(work.key)
                if (
                    self._closed
                    or state is not work.state
                    or state.active is not work.candidate
                ):
                    continue

            image = self._extract(work.candidate.path)
            callbacks: tuple[IconReadyCallback, ...] = ()
            timers_to_cancel: tuple[threading.Timer, ...] = ()
            with self._lock:
                state = self._states.get(work.key)
                if (
                    self._closed
                    or state is not work.state
                    or state.active is not work.candidate
                ):
                    continue
                state.active = None
                if image is not None:
                    # Only successful extractions enter the cache.  A failed
                    # shell lookup remains retryable on this or a later panel
                    # open, and cannot poison a shared extension key.
                    self._cache[work.key] = image
                    callbacks = tuple(state.callbacks)
                    timers_to_cancel = tuple(state.retry_timers)
                    self._drop_state_locked(work.key, state)
                else:
                    candidate = work.candidate
                    if candidate.attempt < len(self._retry_delays):
                        self._schedule_retry_locked(work.key, state, candidate)
                    else:
                        state.known_paths.discard(candidate.identity)
                    # Prefer a different queued source immediately.  A bad
                    # folder or extension representative must never make a
                    # valid same-key candidate wait for its retry backoff.
                    self._enqueue_next_locked(work.key, state)
                    self._drop_state_if_idle_locked(work.key, state)

            for timer in timers_to_cancel:
                timer.cancel()
            if image is not None:
                self._deliver_callbacks(callbacks, work.key, image)

    def _remember_callback_locked(
        self,
        state: _KeyRequestState,
        callback: IconReadyCallback | None,
    ) -> None:
        if callback is not None and all(
            existing is not callback for existing in state.callbacks
        ):
            state.callbacks.append(callback)

    def _enqueue_next_locked(self, key: str, state: _KeyRequestState) -> None:
        if (
            self._closed
            or self._states.get(key) is not state
            or state.active is not None
            or not state.ready
        ):
            return
        candidate = state.ready.popleft()
        state.active = candidate
        self._work_queue.put(_IconWork(key, state, candidate))

    def _schedule_retry_locked(
        self,
        key: str,
        state: _KeyRequestState,
        candidate: _IconCandidate,
    ) -> None:
        retry_candidate = _IconCandidate(
            candidate.path,
            candidate.identity,
            candidate.attempt + 1,
        )
        delay = self._retry_delays[candidate.attempt]
        timer: threading.Timer

        def make_ready() -> None:
            with self._lock:
                state.retry_timers.discard(timer)
                if self._closed or self._states.get(key) is not state:
                    return
                state.ready.append(retry_candidate)
                self._enqueue_next_locked(key, state)

        timer = threading.Timer(delay, make_ready)
        timer.name = f"QuickAccessIconRetry-{candidate.attempt + 1}"
        timer.daemon = True
        state.retry_timers.add(timer)
        timer.start()

    def _drop_state_if_idle_locked(
        self,
        key: str,
        state: _KeyRequestState,
    ) -> None:
        if state.active is None and not state.ready and not state.retry_timers:
            self._drop_state_locked(key, state)

    def _drop_state_locked(self, key: str, state: _KeyRequestState) -> None:
        if self._states.get(key) is state:
            self._states.pop(key, None)
            self._pending.discard(key)
        state.ready.clear()
        state.known_paths.clear()
        state.callbacks.clear()
        state.active = None
        state.retry_timers.clear()

    def _deliver_callbacks(
        self,
        callbacks: tuple[IconReadyCallback, ...],
        key: str,
        image: IconImage,
    ) -> None:
        for callback in callbacks:
            if not self._begin_callback():
                return
            error: Exception | None = None
            try:
                callback(key, image)
            except Exception as caught:
                error = caught
            finally:
                self._end_callback()
            if error is not None:
                self._deliver_callback_error(error)

    def _deliver_callback_error(self, error: Exception) -> None:
        callback = self._on_callback_error
        if callback is None or not self._begin_callback():
            return
        try:
            callback(error)
        except Exception:
            pass
        finally:
            self._end_callback()

    def _begin_callback(self) -> bool:
        ident = threading.get_ident()
        with self._callbacks_idle:
            if self._closed:
                return False
            self._callbacks_inflight += 1
            self._callback_threads[ident] = self._callback_threads.get(ident, 0) + 1
            return True

    def _end_callback(self) -> None:
        ident = threading.get_ident()
        with self._callbacks_idle:
            self._callbacks_inflight -= 1
            remaining = self._callback_threads.get(ident, 0) - 1
            if remaining > 0:
                self._callback_threads[ident] = remaining
            else:
                self._callback_threads.pop(ident, None)
            self._callbacks_idle.notify_all()

    def close(self, timeout: float | None = 1.0) -> bool:
        """Stop retries and workers; return whether every worker has exited.

        An extraction already inside a native API cannot be forcibly aborted.
        It is ignored when it returns, so callbacks cannot begin after close.
        The timeout keeps application shutdown bounded in that exceptional
        case.  Calling close repeatedly is safe.
        """
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be a finite non-negative value or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        current_thread = threading.current_thread()
        timers: tuple[threading.Timer, ...] = ()
        first_close = False

        with self._callbacks_idle:
            if not self._closed:
                first_close = True
                self._closed = True
                timers = tuple(
                    timer
                    for state in self._states.values()
                    for timer in state.retry_timers
                )
                for key, state in tuple(self._states.items()):
                    self._drop_state_locked(key, state)

        if first_close:
            for timer in timers:
                timer.cancel()
            while True:
                try:
                    self._work_queue.get_nowait()
                except queue.Empty:
                    break
            for _worker in self._workers:
                self._work_queue.put(None)

        for worker in self._workers:
            if worker is current_thread:
                continue
            remaining = self._remaining_timeout(deadline)
            if remaining == 0:
                break
            worker.join(remaining)

        current_ident = threading.get_ident()
        with self._callbacks_idle:
            while self._callbacks_inflight > self._callback_threads.get(
                current_ident, 0
            ):
                remaining = self._remaining_timeout(deadline)
                if remaining == 0:
                    break
                self._callbacks_idle.wait(remaining)

        return all(
            worker is current_thread or not worker.is_alive()
            for worker in self._workers
        )

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def __enter__(self) -> IconService:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _extract(self, path: str) -> IconImage | None:
        try:
            api = self._api
            return None if api is None else api.extract(path, self._size)  # type: ignore[union-attr]
        except Exception:
            return None


__all__ = ["IconImage", "IconService", "icon_key"]
