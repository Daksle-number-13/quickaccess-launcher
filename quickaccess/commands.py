"""Thread-safe commands used to cross application thread boundaries.

Background integrations (global hotkeys, the system tray and COM workers) must
not call Tk directly.  They publish one of the immutable commands in this
module, and the Tk thread drains :class:`CommandBus` from its event loop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import queue
import threading
import time
from typing import TypeAlias


class CommandType(str, Enum):
    """Stable command identifiers, suitable for logging and diagnostics."""

    OPEN_PANEL = "open_panel"
    OPEN_SETTINGS = "open_settings"
    QUICK_ADD = "quick_add"
    QUICK_ADD_RESULT = "quick_add_result"
    VALIDATION_RESULT = "validation_result"
    LAUNCH_RESULT = "launch_result"
    SHOW_TOAST = "show_toast"
    UPDATE_AVAILABLE = "update_available"
    ICON_READY = "icon_ready"
    QUIT = "quit"


class CommandSource(str, Enum):
    """Origin of a command."""

    SYSTEM = "system"
    HOTKEY = "hotkey"
    TRAY = "tray"
    UI = "ui"
    WORKER = "worker"


class ToastLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenPanelCommand:
    """Ask the UI thread to open or toggle the launcher panel."""

    source: CommandSource = CommandSource.SYSTEM
    cursor_position: tuple[int, int] | None = None
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.OPEN_PANEL, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenSettingsCommand:
    """Ask the UI thread to display the settings window."""

    source: CommandSource = CommandSource.SYSTEM
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.OPEN_SETTINGS, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class QuickAddCommand:
    """Ask the Explorer integration to resolve a quick-add target.

    ``explorer_hwnd`` should be captured at hotkey time.  Delaying that lookup
    until a worker runs can resolve a different foreground window.
    """

    source: CommandSource = CommandSource.HOTKEY
    explorer_hwnd: int | None = None
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.QUICK_ADD, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationResultCommand:
    """Deliver a path-validation DTO back to the Tk thread."""

    result: object
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.VALIDATION_RESULT, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class QuickAddResultCommand:
    """Deliver an Explorer COM lookup result back to the Tk thread."""

    result: object
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.QUICK_ADD_RESULT, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchResultCommand:
    """Deliver a shell-launch result back to the Tk thread."""

    item_name: str
    result: object
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.LAUNCH_RESULT, init=False)

    def __post_init__(self) -> None:
        _require_text(self.item_name, "item_name")


@dataclass(frozen=True, slots=True, kw_only=True)
class ShowToastCommand:
    """Ask the UI thread to show a non-fatal user notification."""

    message: str
    title: str | None = None
    level: ToastLevel = ToastLevel.INFO
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.SHOW_TOAST, init=False)

    def __post_init__(self) -> None:
        _require_text(self.message, "message")


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateAvailableCommand:
    """Deliver a background release-check result back to the Tk thread."""

    result: object
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.UPDATE_AVAILABLE, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class IconReadyCommand:
    """Deliver a decoded shell icon bitmap back to the Tk thread."""

    key: str
    image: object
    source: CommandSource = CommandSource.WORKER
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.ICON_READY, init=False)

    def __post_init__(self) -> None:
        _require_text(self.key, "key")


@dataclass(frozen=True, slots=True, kw_only=True)
class QuitCommand:
    """Ask the UI thread to perform the coordinated shutdown sequence."""

    source: CommandSource = CommandSource.SYSTEM
    reason: str | None = None
    created_at: float = field(default_factory=time.monotonic, compare=False)
    type: CommandType = field(default=CommandType.QUIT, init=False)


AppCommand: TypeAlias = (
    OpenPanelCommand
    | OpenSettingsCommand
    | QuickAddCommand
    | ValidationResultCommand
    | QuickAddResultCommand
    | LaunchResultCommand
    | ShowToastCommand
    | UpdateAvailableCommand
    | IconReadyCommand
    | QuitCommand
)


class CommandBusClosedError(RuntimeError):
    """Raised when a caller publishes to, or waits on, a closed bus."""


@dataclass(slots=True)
class _QueuedCommand:
    sequence: int
    command: AppCommand
    priority: int
    coalescing_key: tuple[object, ...] | None
    consumed: bool = False


class CommandBus:
    """A small FIFO command channel safe for multiple publisher threads.

    Closing the bus wakes blocked consumers.  Commands already queued at close
    time remain drainable, which lets the application finish an orderly
    shutdown without losing earlier work.
    """

    def __init__(self) -> None:
        self._fifo: deque[_QueuedCommand] = deque()
        self._priority_queues: tuple[deque[_QueuedCommand], ...] = tuple(
            deque() for _ in range(5)
        )
        self._latest_by_key: dict[tuple[object, ...], _QueuedCommand] = {}
        self._next_sequence = 0
        self._size = 0
        self._closed = False
        self._condition = threading.Condition()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, command: AppCommand) -> None:
        """Append ``command`` and wake one waiting consumer."""

        with self._condition:
            if self._closed:
                raise CommandBusClosedError("command bus is closed")
            self._append_locked(command)
            self._condition.notify()

    def publish_many(self, commands: tuple[AppCommand, ...] | list[AppCommand]) -> None:
        """Atomically append several commands while preserving their order."""

        if not commands:
            return
        with self._condition:
            if self._closed:
                raise CommandBusClosedError("command bus is closed")
            for command in commands:
                self._append_locked(command)
            self._condition.notify_all()

    def get(self, *, block: bool = True, timeout: float | None = None) -> AppCommand:
        """Return the next command.

        The method follows :class:`queue.Queue` conventions: an empty,
        non-blocking read or an elapsed timeout raises :class:`queue.Empty`.
        Once a closed bus has no pending commands, it raises
        :class:`CommandBusClosedError` instead.
        """

        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")

        with self._condition:
            if not block:
                return self._pop_or_raise()

            deadline = None if timeout is None else time.monotonic() + timeout
            while self._size == 0 and not self._closed:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)

            return self._pop_or_raise()

    def get_nowait(self) -> AppCommand:
        return self.get(block=False)

    def drain(self, max_items: int | None = None) -> list[AppCommand]:
        """Remove and return currently queued commands without blocking."""

        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative")
        if max_items == 0:
            return []

        with self._condition:
            count = self._size if max_items is None else min(self._size, max_items)
            drained: list[AppCommand] = []
            while len(drained) < count:
                envelope = self._pop_fifo_envelope_locked()
                if envelope is None:
                    break
                self._consume_locked(envelope)
                drained.append(envelope.command)
            return drained

    def drain_for_ui(self, max_items: int | None = None) -> list[AppCommand]:
        """Drain commands by interaction priority with safe coalescing.

        The resident UI receives many low-priority validation and icon
        results.  A hotkey request must never wait behind that backlog.  This
        method keeps :meth:`drain`'s public FIFO contract intact while giving
        the application a latency-oriented view that:

        - handles quit and panel requests before background presentation data;
        - keeps only the newest command for idempotent/coalescible targets;
        - preserves publication order inside each priority tier.
        """

        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative")
        if max_items == 0:
            return []

        with self._condition:
            if self._size == 0:
                return []

            count = self._size if max_items is None else min(self._size, max_items)
            drained: list[AppCommand] = []
            for priority_queue in self._priority_queues:
                while priority_queue and len(drained) < count:
                    envelope = priority_queue.popleft()
                    if envelope.consumed:
                        continue
                    key = envelope.coalescing_key
                    if key is not None and self._latest_by_key.get(key) is not envelope:
                        self._consume_locked(envelope)
                        continue
                    self._consume_locked(envelope)
                    drained.append(envelope.command)
                if len(drained) >= count:
                    break
            self._discard_consumed_fifo_head_locked()
            return drained

    def close(self, *, discard_pending: bool = False) -> None:
        """Reject future publishes and wake every blocked consumer."""

        with self._condition:
            if discard_pending:
                self._fifo.clear()
                for priority_queue in self._priority_queues:
                    priority_queue.clear()
                self._latest_by_key.clear()
                self._size = 0
            self._closed = True
            self._condition.notify_all()

    def empty(self) -> bool:
        with self._condition:
            return self._size == 0

    def __len__(self) -> int:
        with self._condition:
            return self._size

    def _pop_or_raise(self) -> AppCommand:
        envelope = self._pop_fifo_envelope_locked()
        if envelope is not None:
            self._consume_locked(envelope)
            return envelope.command
        if self._closed:
            raise CommandBusClosedError("command bus is closed")
        raise queue.Empty

    def _append_locked(self, command: AppCommand) -> None:
        priority = self._ui_priority(command)
        key = self._coalescing_key(command)
        envelope = _QueuedCommand(
            sequence=self._next_sequence,
            command=command,
            priority=priority,
            coalescing_key=key,
        )
        self._next_sequence += 1
        self._size += 1
        self._fifo.append(envelope)
        self._priority_queues[priority].append(envelope)
        if key is not None:
            self._latest_by_key[key] = envelope

    def _pop_fifo_envelope_locked(self) -> _QueuedCommand | None:
        self._discard_consumed_fifo_head_locked()
        if not self._fifo:
            return None
        return self._fifo.popleft()

    def _discard_consumed_fifo_head_locked(self) -> None:
        while self._fifo and self._fifo[0].consumed:
            self._fifo.popleft()

    def _consume_locked(self, envelope: _QueuedCommand) -> None:
        if envelope.consumed:
            return
        envelope.consumed = True
        self._size -= 1
        key = envelope.coalescing_key
        if key is not None and self._latest_by_key.get(key) is envelope:
            self._latest_by_key.pop(key, None)

    @staticmethod
    def _ui_priority(command: AppCommand) -> int:
        if isinstance(command, QuitCommand):
            return 0
        if isinstance(command, OpenPanelCommand):
            return 1
        if isinstance(command, (OpenSettingsCommand, QuickAddCommand)):
            return 2
        if isinstance(command, (ValidationResultCommand, IconReadyCommand)):
            return 4
        return 3

    @staticmethod
    def _coalescing_key(command: AppCommand) -> tuple[object, ...] | None:
        if isinstance(command, OpenPanelCommand):
            return (CommandType.OPEN_PANEL,)
        if isinstance(command, OpenSettingsCommand):
            return (CommandType.OPEN_SETTINGS,)
        if isinstance(command, UpdateAvailableCommand):
            return (CommandType.UPDATE_AVAILABLE,)
        if isinstance(command, IconReadyCommand):
            return (CommandType.ICON_READY, command.key)
        if isinstance(command, ValidationResultCommand):
            item_id = getattr(command.result, "item_id", None)
            if item_id is not None:
                return (CommandType.VALIDATION_RESULT, item_id)
        return None
