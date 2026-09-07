"""Non-blocking path validation with per-item deadlines and generations."""

from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum


class PathStatus(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    item_id: str
    path: str
    generation: int
    status: PathStatus
    elapsed_seconds: float
    error: str | None = None

    @property
    def is_broken(self) -> bool:
        # A deadline only means Windows did not answer quickly enough.  The
        # target may still be launchable (notably sleeping network/OneDrive
        # paths), so TIMEOUT is a warning rather than a broken shortcut.
        return self.status in {PathStatus.MISSING, PathStatus.ERROR}


@dataclass(slots=True)
class _TaskState:
    item_id: str
    path: str
    generation: int
    queued_at: float
    callback: Callable[[ValidationResult], object]
    started_at: float | None = None
    done: bool = False
    retire_worker: bool = False


class PathValidationService:
    """Validate paths without blocking Tk or process shutdown.

    Windows filesystem calls cannot be forcibly interrupted from Python.  A
    small daemon worker pool caps ordinary concurrency, while one watchdog
    thread emits timeout results.  A bounded number of replacement workers
    preserves service for later local paths if an OS call never returns.
    Network-looking paths receive lower queue priority.  A late probe result
    is ignored, and generation checks prevent an old path from overwriting a
    re-assigned item.
    """

    def __init__(
        self,
        on_result: Callable[[ValidationResult], object] | None = None,
        *,
        timeout_seconds: float = 2.0,
        queue_timeout_seconds: float | None = None,
        exists: Callable[[str], object] = os.path.exists,
        clock: Callable[[], float] = time.monotonic,
        on_callback_error: Callable[[Exception], object] | None = None,
        max_workers: int = 4,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive value")
        if queue_timeout_seconds is not None and (
            not math.isfinite(queue_timeout_seconds) or queue_timeout_seconds <= 0
        ):
            raise ValueError(
                "queue_timeout_seconds must be a finite positive value"
            )
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a positive integer")
        self._default_callback = on_result
        self._timeout_seconds = float(timeout_seconds)
        self._queue_timeout_seconds = float(
            queue_timeout_seconds
            if queue_timeout_seconds is not None
            else max(0.5, timeout_seconds * 2.0)
        )
        self._exists = exists
        self._clock = clock
        self._on_callback_error = on_callback_error
        self._max_workers = int(max_workers)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._work_queue: queue.PriorityQueue[
            tuple[int, int, _TaskState | None]
        ] = queue.PriorityQueue()
        self._workers_started = False
        self._worker_serial = 0
        self._queue_serial = 0
        self._workers: list[threading.Thread] = []
        self._stranded_workers = 0
        self._max_stranded_workers = self._max_workers
        self._generations: dict[str, int] = {}
        self._active: dict[str, _TaskState] = {}
        self._closed = False

    def validate(
        self,
        item_id: str,
        path: str,
        callback: Callable[[ValidationResult], object] | None = None,
    ) -> int:
        if not item_id:
            raise ValueError("item_id must be non-empty")
        if callback is None:
            callback = self._default_callback
        if callback is None:
            raise ValueError("a result callback is required")

        with self._lock:
            if self._closed:
                raise RuntimeError("path validator is closed")
            previous = self._active.pop(item_id, None)
            if previous is not None:
                previous.done = True
                self._retire_probe_locked(previous)

            generation = self._generations.get(item_id, 0) + 1
            self._generations[item_id] = generation
            state = _TaskState(item_id, path, generation, self._clock(), callback)
            self._active[item_id] = state
            self._ensure_workers_locked()
            self._queue_serial += 1
            self._work_queue.put(
                (_path_queue_priority(path), self._queue_serial, state)
            )
            self._condition.notify_all()
            return generation

    def validate_many(
        self,
        items: Mapping[str, str] | Iterable[tuple[str, str]],
        callback: Callable[[ValidationResult], object] | None = None,
    ) -> dict[str, int]:
        pairs = items.items() if isinstance(items, Mapping) else items
        return {
            item_id: self.validate(item_id, path, callback)
            for item_id, path in pairs
        }

    def cancel(self, item_id: str) -> None:
        with self._lock:
            self._generations[item_id] = self._generations.get(item_id, 0) + 1
            state = self._active.pop(item_id, None)
            if state is not None:
                state.done = True
                self._retire_probe_locked(state)
            self._condition.notify_all()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            states = list(self._active.values())
            self._active.clear()
            for state in states:
                state.done = True
                self._generations[state.item_id] = state.generation + 1
            self._condition.notify_all()
            if self._workers_started:
                for _worker in self._workers:
                    self._queue_serial += 1
                    self._work_queue.put((-1, self._queue_serial, None))

    def generation(self, item_id: str) -> int:
        with self._lock:
            return self._generations.get(item_id, 0)

    def _ensure_workers_locked(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        for _index in range(self._max_workers):
            self._start_worker_locked()
        threading.Thread(
            target=self._watchdog_loop,
            name="QuickAccessPathWatchdog",
            daemon=True,
        ).start()

    def _start_worker_locked(self) -> None:
        self._worker_serial += 1
        worker = threading.Thread(
            target=self._worker_loop,
            name=f"QuickAccessPathWorker-{self._worker_serial}",
            daemon=True,
        )
        self._workers.append(worker)
        worker.start()

    def _worker_loop(self) -> None:
        while True:
            _priority, _sequence, state = self._work_queue.get()
            if state is None:
                return
            with self._lock:
                should_probe = (
                    not self._closed
                    and not state.done
                    and self._active.get(state.item_id) is state
                    and self._generations.get(state.item_id) == state.generation
                )
                if should_probe:
                    # Queue residence is deliberately excluded from the path
                    # deadline.  A request cannot time out before a worker has
                    # actually attempted the filesystem call.
                    state.started_at = self._clock()
                    self._condition.notify_all()
            if should_probe:
                self._probe(state)
            with self._lock:
                if state.retire_worker:
                    self._stranded_workers = max(0, self._stranded_workers - 1)
                    current = threading.current_thread()
                    if current in self._workers:
                        self._workers.remove(current)
                    self._condition.notify_all()
                    return

    def _watchdog_loop(self) -> None:
        while True:
            expired_probes: list[_TaskState] = []
            expired_queued: list[_TaskState] = []
            with self._condition:
                if self._closed:
                    return
                live = [
                    state
                    for state in self._active.values()
                    if not state.done
                ]
                if not live:
                    self._condition.wait()
                    continue
                now = self._clock()
                nearest_deadline = min(
                    (
                        state.started_at + self._timeout_seconds
                        if state.started_at is not None
                        else state.queued_at + self._queue_timeout_seconds
                    )
                    for state in live
                )
                remaining = nearest_deadline - now
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                expired_probes = [
                    state
                    for state in live
                    if state.started_at is not None
                    and state.started_at + self._timeout_seconds <= now
                ]
                expired_queued = [
                    state
                    for state in live
                    if state.started_at is None
                    and state.queued_at + self._queue_timeout_seconds <= now
                ]
            for state in expired_probes:
                self._on_timeout(state)
            for state in expired_queued:
                self._on_queue_timeout(state)

    def _probe(self, state: _TaskState) -> None:
        try:
            exists = bool(self._exists(state.path))
            status = PathStatus.VALID if exists else PathStatus.MISSING
            error = None
        except Exception as exception:
            status = PathStatus.ERROR
            error = str(exception)
        self._finish(state, status, error)

    def _on_timeout(self, state: _TaskState) -> None:
        with self._lock:
            if (
                self._closed
                or state.done
                or state.started_at is None
                or self._active.get(state.item_id) is not state
                or self._generations.get(state.item_id) != state.generation
            ):
                return
            self._retire_probe_locked(state)
        self._finish(
            state,
            PathStatus.TIMEOUT,
            f"path check exceeded {self._timeout_seconds:g} seconds",
        )

    def _on_queue_timeout(self, state: _TaskState) -> None:
        """Return an unknown warning when no worker can begin the probe."""

        with self._lock:
            if (
                self._closed
                or state.done
                or state.started_at is not None
                or self._active.get(state.item_id) is not state
                or self._generations.get(state.item_id) != state.generation
            ):
                return
        self._finish(
            state,
            PathStatus.TIMEOUT,
            "path check could not start because validation workers remained busy",
        )

    def _retire_probe_locked(self, state: _TaskState) -> None:
        """Replace a stuck worker once, subject to a strict stranded cap."""

        if (
            state.started_at is None
            or state.retire_worker
            or self._closed
            or self._stranded_workers >= self._max_stranded_workers
        ):
            return
        state.retire_worker = True
        self._stranded_workers += 1
        self._start_worker_locked()

    def _finish(
        self,
        state: _TaskState,
        status: PathStatus,
        error: str | None,
    ) -> None:
        with self._lock:
            if self._closed or state.done:
                return
            if self._generations.get(state.item_id) != state.generation:
                return
            if self._active.get(state.item_id) is not state:
                return
            state.done = True
            self._active.pop(state.item_id, None)
            self._condition.notify_all()
            started_at = state.started_at
            result = ValidationResult(
                item_id=state.item_id,
                path=state.path,
                generation=state.generation,
                status=status,
                elapsed_seconds=max(
                    0.0,
                    self._clock()
                    - (started_at if started_at is not None else state.queued_at),
                ),
                error=error,
            )
            callback = state.callback

        try:
            callback(result)
        except Exception as callback_error:
            if self._on_callback_error is not None:
                try:
                    self._on_callback_error(callback_error)
                except Exception:
                    pass

    def __enter__(self) -> "PathValidationService":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _path_queue_priority(path: str) -> int:
    """Prefer local work over UNC/device paths when replacements are scarce."""

    normalized = path.strip().replace("/", "\\").lower()
    return int(
        normalized.startswith("\\\\")
        or normalized.startswith(("\\\\?\\", "\\\\.\\"))
    )


__all__ = ["PathStatus", "PathValidationService", "ValidationResult"]
