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
    | QuitCommand
)


class CommandBusClosedError(RuntimeError):
    """Raised when a caller publishes to, or waits on, a closed bus."""


class CommandBus:
    """A small FIFO command channel safe for multiple publisher threads.

    Closing the bus wakes blocked consumers.  Commands already queued at close
    time remain drainable, which lets the application finish an orderly
    shutdown without losing earlier work.
    """

    def __init__(self) -> None:
        self._commands: deque[AppCommand] = deque()
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
            self._commands.append(command)
            self._condition.notify()

    def publish_many(self, commands: tuple[AppCommand, ...] | list[AppCommand]) -> None:
        """Atomically append several commands while preserving their order."""

        if not commands:
            return
        with self._condition:
            if self._closed:
                raise CommandBusClosedError("command bus is closed")
            self._commands.extend(commands)
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
            while not self._commands and not self._closed:
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
            count = len(self._commands) if max_items is None else min(
                len(self._commands), max_items
            )
            return [self._commands.popleft() for _ in range(count)]

    def close(self, *, discard_pending: bool = False) -> None:
        """Reject future publishes and wake every blocked consumer."""

        with self._condition:
            if discard_pending:
                self._commands.clear()
            self._closed = True
            self._condition.notify_all()

    def empty(self) -> bool:
        with self._condition:
            return not self._commands

    def __len__(self) -> int:
        with self._condition:
            return len(self._commands)

    def _pop_or_raise(self) -> AppCommand:
        if self._commands:
            return self._commands.popleft()
        if self._closed:
            raise CommandBusClosedError("command bus is closed")
        raise queue.Empty
