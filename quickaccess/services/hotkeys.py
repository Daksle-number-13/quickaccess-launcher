"""Native Windows global hotkeys with a dedicated message-pump thread.

Only the callback itself runs on the hotkey thread.  Application callbacks
should enqueue a command for the Tk thread and return promptly.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_APP = 0x8000
_WM_COMMAND = WM_APP + 0x51
_PM_NOREMOVE = 0x0000
_BASE_HOTKEY_ID = 0x5100


class HotkeyError(RuntimeError):
    pass


class HotkeyParseError(ValueError):
    pass


class HotkeyUnavailableError(HotkeyError):
    pass


class HotkeyRegistrationError(HotkeyError):
    def __init__(self, message: str, *, rollback_succeeded: bool = True) -> None:
        super().__init__(message)
        self.rollback_succeeded = rollback_succeeded


_MODIFIER_ALIASES = {
    "ctrl": ("ctrl", MOD_CONTROL),
    "control": ("ctrl", MOD_CONTROL),
    "alt": ("alt", MOD_ALT),
    "option": ("alt", MOD_ALT),
    "shift": ("shift", MOD_SHIFT),
    "win": ("win", MOD_WIN),
    "windows": ("win", MOD_WIN),
    "super": ("win", MOD_WIN),
}
_MODIFIER_ORDER = (
    ("ctrl", MOD_CONTROL),
    ("alt", MOD_ALT),
    ("shift", MOD_SHIFT),
    ("win", MOD_WIN),
)
_NAMED_KEYS = {
    "backspace": ("backspace", 0x08),
    "tab": ("tab", 0x09),
    "enter": ("enter", 0x0D),
    "return": ("enter", 0x0D),
    "pause": ("pause", 0x13),
    "capslock": ("capslock", 0x14),
    "caps lock": ("capslock", 0x14),
    "esc": ("esc", 0x1B),
    "escape": ("esc", 0x1B),
    "space": ("space", 0x20),
    "pageup": ("pageup", 0x21),
    "page up": ("pageup", 0x21),
    "pagedown": ("pagedown", 0x22),
    "page down": ("pagedown", 0x22),
    "end": ("end", 0x23),
    "home": ("home", 0x24),
    "left": ("left", 0x25),
    "up": ("up", 0x26),
    "right": ("right", 0x27),
    "down": ("down", 0x28),
    "printscreen": ("printscreen", 0x2C),
    "print screen": ("printscreen", 0x2C),
    "insert": ("insert", 0x2D),
    "delete": ("delete", 0x2E),
    "del": ("delete", 0x2E),
    ";": (";", 0xBA),
    "=": ("=", 0xBB),
    ",": (",", 0xBC),
    "-": ("-", 0xBD),
    ".": (".", 0xBE),
    "/": ("/", 0xBF),
    "`": ("`", 0xC0),
    "[": ("[", 0xDB),
    "\\": ("\\", 0xDC),
    "]": ("]", 0xDD),
    "'": ("'", 0xDE),
}


@dataclass(frozen=True, slots=True)
class ParsedHotkey:
    modifiers: int
    virtual_key: int
    canonical: str

    @property
    def registration_modifiers(self) -> int:
        return self.modifiers | MOD_NOREPEAT

    @property
    def identity(self) -> tuple[int, int]:
        return self.modifiers, self.virtual_key


@dataclass(frozen=True, slots=True)
class HotkeyBinding:
    shortcut: str
    callback: Callable[[], object]


@dataclass(frozen=True, slots=True)
class PreparedHotkeyBinding:
    name: str
    hotkey: ParsedHotkey
    callback: Callable[[], object]


BindingInput: TypeAlias = HotkeyBinding | tuple[str, Callable[[], object]]


def _parse_key(token: str) -> tuple[str, int]:
    if token in _NAMED_KEYS:
        return _NAMED_KEYS[token]
    if len(token) == 1 and ("a" <= token <= "z" or "0" <= token <= "9"):
        return token, ord(token.upper())
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return f"f{number}", 0x70 + number - 1
    if token.startswith("numpad") and token[6:].isdigit():
        number = int(token[6:])
        if 0 <= number <= 9:
            return f"numpad{number}", 0x60 + number
    raise HotkeyParseError(f"unsupported hotkey key: {token!r}")


def parse_hotkey(shortcut: str) -> ParsedHotkey:
    """Parse a user-facing shortcut such as ``ctrl+shift+space``."""

    if not isinstance(shortcut, str) or not shortcut.strip():
        raise HotkeyParseError("hotkey must be a non-empty string")
    tokens = [token.strip().casefold() for token in shortcut.split("+")]
    if any(not token for token in tokens):
        raise HotkeyParseError("hotkey contains an empty component")

    modifiers = 0
    modifier_names: set[str] = set()
    key: tuple[str, int] | None = None
    for token in tokens:
        modifier = _MODIFIER_ALIASES.get(token)
        if modifier is not None:
            name, flag = modifier
            if name in modifier_names:
                raise HotkeyParseError(f"duplicate modifier: {name}")
            modifier_names.add(name)
            modifiers |= flag
            continue
        if key is not None:
            raise HotkeyParseError("hotkey must contain exactly one non-modifier key")
        key = _parse_key(token)

    if key is None:
        raise HotkeyParseError("hotkey must contain a non-modifier key")
    if not modifiers:
        raise HotkeyParseError("a global hotkey must contain at least one modifier")

    key_name, virtual_key = key
    canonical_parts = [name for name, flag in _MODIFIER_ORDER if modifiers & flag]
    canonical_parts.append(key_name)
    return ParsedHotkey(modifiers, virtual_key, "+".join(canonical_parts))


# Combinations known to collide with software QuickAccess cannot detect via
# RegisterHotKey, because they are consumed before Windows dispatches
# WM_HOTKEY (IME toggles) or are merely a strong convention (IDE
# autocomplete).  RegisterHotKey only fails for a hotkey another app has
# *also* registered natively, so these silent collisions need a static hint
# instead of a runtime probe.
KNOWN_HOTKEY_CONFLICTS: Mapping[str, str] = {
    "ctrl+space": "Windows 한/영 전환 및 일부 IDE(Ctrl+Space 자동완성)와 겹칠 수 있습니다.",
    "ctrl+shift+space": "일부 IDE의 매개변수 정보 단축키와 겹칠 수 있습니다.",
}


def describe_hotkey_conflict_risk(shortcut: str) -> str | None:
    """Return a static caution for a shortcut known to commonly collide.

    This does not detect an actual runtime conflict (RegisterHotKey already
    surfaces those as a registration error).  It flags combinations that
    silently coexist with QuickAccess but may not fire as expected.
    """

    try:
        canonical = parse_hotkey(shortcut).canonical
    except HotkeyParseError:
        return None
    return KNOWN_HOTKEY_CONFLICTS.get(canonical)


def prepare_bindings(bindings: Mapping[str, BindingInput]) -> tuple[PreparedHotkeyBinding, ...]:
    """Validate and canonicalize a complete binding set before native mutation."""

    prepared: list[PreparedHotkeyBinding] = []
    identities: dict[tuple[int, int], str] = {}
    for name, value in bindings.items():
        if not isinstance(name, str) or not name:
            raise HotkeyParseError("binding names must be non-empty strings")
        if isinstance(value, HotkeyBinding):
            shortcut, callback = value.shortcut, value.callback
        else:
            try:
                shortcut, callback = value
            except (TypeError, ValueError) as error:
                raise HotkeyParseError(f"invalid binding for {name!r}") from error
        if not callable(callback):
            raise HotkeyParseError(f"callback for {name!r} is not callable")
        hotkey = parse_hotkey(shortcut)
        duplicate = identities.get(hotkey.identity)
        if duplicate is not None:
            raise HotkeyParseError(
                f"{name!r} duplicates {duplicate!r}: {hotkey.canonical}"
            )
        identities[hotkey.identity] = name
        prepared.append(PreparedHotkeyBinding(name, hotkey, callback))
    return tuple(prepared)


class _NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", _NativePoint),
        ("lPrivate", ctypes.c_ulong),
    ]


class _NativeHotkeyApi:
    def __init__(self) -> None:
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise HotkeyUnavailableError("RegisterHotKey is available only on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self.user32.RegisterHotKey.restype = ctypes.c_bool
        self.user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = ctypes.c_bool
        self.user32.PostThreadMessageW.argtypes = [
            ctypes.c_ulong,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self.user32.PostThreadMessageW.restype = ctypes.c_bool
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(_Message),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self.user32.PeekMessageW.restype = ctypes.c_bool
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(_Message),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    def create_message_queue(self) -> int:
        message = _Message()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, _PM_NOREMOVE)
        return int(self.kernel32.GetCurrentThreadId())

    def register(self, hotkey_id: int, hotkey: ParsedHotkey) -> None:
        ctypes.set_last_error(0)
        if not self.user32.RegisterHotKey(
            None,
            hotkey_id,
            hotkey.registration_modifiers,
            hotkey.virtual_key,
        ):
            self._raise_last_error(f"RegisterHotKey({hotkey.canonical})")

    def unregister(self, hotkey_id: int) -> None:
        ctypes.set_last_error(0)
        if not self.user32.UnregisterHotKey(None, hotkey_id):
            self._raise_last_error(f"UnregisterHotKey({hotkey_id})")

    def post(self, thread_id: int, message: int) -> None:
        ctypes.set_last_error(0)
        if not self.user32.PostThreadMessageW(thread_id, message, 0, 0):
            self._raise_last_error("PostThreadMessageW")

    def get_message(self) -> tuple[int, _Message]:
        message = _Message()
        result = int(self.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
        if result == -1:
            self._raise_last_error("GetMessageW")
        return result, message

    @staticmethod
    def _raise_last_error(operation: str) -> None:
        error_code = ctypes.get_last_error()
        error = ctypes.WinError(error_code)
        raise OSError(error_code, f"{operation} failed: {error}")


@dataclass(frozen=True, slots=True)
class _Registration:
    hotkey_id: int
    binding: PreparedHotkeyBinding


class _RegistrationManager:
    """Thread-confined native registration set with best-effort rollback."""

    def __init__(self, api: object) -> None:
        self.api = api
        self.registrations: tuple[_Registration, ...] = ()

    def apply(self, desired: tuple[PreparedHotkeyBinding, ...]) -> None:
        if self._same_layout(desired):
            self.registrations = tuple(
                _Registration(existing.hotkey_id, binding)
                for existing, binding in zip(self.registrations, desired)
            )
            return

        old = self.registrations
        removed: list[_Registration] = []
        try:
            for registration in old:
                self.api.unregister(registration.hotkey_id)
                removed.append(registration)
        except Exception as error:
            rollback_ok = self._register_all(removed)
            raise HotkeyRegistrationError(
                f"could not replace existing hotkeys: {error}",
                rollback_succeeded=rollback_ok,
            ) from error

        registered: list[_Registration] = []
        try:
            for index, binding in enumerate(desired):
                registration = _Registration(_BASE_HOTKEY_ID + index, binding)
                self.api.register(registration.hotkey_id, binding.hotkey)
                registered.append(registration)
        except Exception as error:
            cleanup_ok = self._unregister_all(registered)
            rollback_ok = self._register_all(old)
            if rollback_ok:
                self.registrations = old
            else:
                self.registrations = ()
            raise HotkeyRegistrationError(
                f"could not register new hotkeys: {error}",
                rollback_succeeded=cleanup_ok and rollback_ok,
            ) from error
        self.registrations = tuple(registered)

    def clear(self) -> None:
        registrations, self.registrations = self.registrations, ()
        errors: list[Exception] = []
        for registration in registrations:
            try:
                self.api.unregister(registration.hotkey_id)
            except Exception as error:
                errors.append(error)
        if errors:
            raise HotkeyRegistrationError(f"failed to unregister hotkeys: {errors[0]}")

    def callback_for(self, hotkey_id: int) -> Callable[[], object] | None:
        for registration in self.registrations:
            if registration.hotkey_id == hotkey_id:
                return registration.binding.callback
        return None

    def snapshot(self) -> dict[str, str]:
        return {
            registration.binding.name: registration.binding.hotkey.canonical
            for registration in self.registrations
        }

    def _same_layout(self, desired: tuple[PreparedHotkeyBinding, ...]) -> bool:
        if len(desired) != len(self.registrations):
            return False
        return all(
            current.binding.name == replacement.name
            and current.binding.hotkey.identity == replacement.hotkey.identity
            for current, replacement in zip(self.registrations, desired)
        )

    def _register_all(self, registrations: list[_Registration] | tuple[_Registration, ...]) -> bool:
        successful: list[_Registration] = []
        for registration in registrations:
            try:
                self.api.register(registration.hotkey_id, registration.binding.hotkey)
                successful.append(registration)
            except Exception:
                self._unregister_all(successful)
                return False
        return True

    def _unregister_all(
        self,
        registrations: list[_Registration] | tuple[_Registration, ...],
    ) -> bool:
        succeeded = True
        for registration in reversed(registrations):
            try:
                self.api.unregister(registration.hotkey_id)
            except Exception:
                succeeded = False
        return succeeded


@dataclass(slots=True)
class _Command:
    operation: str
    payload: object = None
    done: threading.Event | None = None
    result: object = None
    error: BaseException | None = None


class NativeHotkeyService:
    """Own RegisterHotKey registrations and their Windows message pump."""

    def __init__(
        self,
        *,
        on_callback_error: Callable[[Exception], object] | None = None,
        command_timeout: float = 5.0,
        api_factory: Callable[[], object] = _NativeHotkeyApi,
    ) -> None:
        if command_timeout <= 0:
            raise ValueError("command_timeout must be greater than zero")
        self._on_callback_error = on_callback_error
        self._command_timeout = command_timeout
        self._api_factory = api_factory
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._submit_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._api: object | None = None
        self._manager: _RegistrationManager | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._snapshot: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def bindings(self) -> dict[str, str]:
        with self._state_lock:
            return dict(self._snapshot)

    def start(self) -> None:
        with self._lifecycle_lock:
            if not self.running:
                self._ready.clear()
                self._startup_error = None
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="QuickAccessHotkeys",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(self._command_timeout):
            raise HotkeyUnavailableError("hotkey message thread did not start in time")
        if self._startup_error is not None:
            raise HotkeyUnavailableError(f"hotkey service failed to start: {self._startup_error}")

    def configure(self, bindings: Mapping[str, BindingInput]) -> dict[str, str]:
        """Replace the complete binding set and restore the old set on failure."""

        desired = prepare_bindings(bindings)
        self.start()
        result = self._submit(_Command("configure", desired, threading.Event()))
        return dict(result)

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
        if thread.ident == threading.get_ident():
            raise HotkeyError("stop must not be called from a hotkey callback")
        if thread.is_alive():
            try:
                self._submit(_Command("stop", done=threading.Event()))
            except Exception:
                pass
            thread.join(self._command_timeout)
        with self._lifecycle_lock:
            if not thread.is_alive():
                self._thread = None

    close = stop

    def _submit(self, command: _Command) -> object:
        with self._submit_lock:
            thread = self._thread
            if (
                thread is None
                or not thread.is_alive()
                or self._api is None
                or self._thread_id is None
            ):
                raise HotkeyUnavailableError("hotkey service is not running")
            if thread.ident == threading.get_ident():
                raise HotkeyError("configuration must not run from a hotkey callback")
            self._commands.put(command)
            try:
                self._api.post(self._thread_id, _WM_COMMAND)
            except Exception:
                try:
                    self._commands.get_nowait()
                except queue.Empty:
                    pass
                raise
            if command.done is None or not command.done.wait(self._command_timeout):
                raise HotkeyUnavailableError("hotkey command timed out")
            if command.error is not None:
                raise command.error
            return command.result

    def _thread_main(self) -> None:
        manager: _RegistrationManager | None = None
        try:
            api = self._api_factory()
            thread_id = api.create_message_queue()
            manager = _RegistrationManager(api)
            self._api = api
            self._thread_id = thread_id
            self._manager = manager
            self._ready.set()

            stop_requested = False
            while not stop_requested:
                result, message = api.get_message()
                if result == 0:
                    break
                if int(message.message) == _WM_COMMAND:
                    stop_requested = self._drain_commands(manager)
                elif int(message.message) == WM_HOTKEY:
                    callback = manager.callback_for(int(message.wParam))
                    if callback is not None:
                        self._run_callback(callback)
        except BaseException as error:
            self._startup_error = error
            self._ready.set()
            self._fail_pending_commands(error)
        finally:
            if manager is not None:
                try:
                    manager.clear()
                except Exception:
                    pass
            with self._state_lock:
                self._snapshot = {}
            self._api = None
            self._thread_id = None
            self._manager = None

    def _drain_commands(self, manager: _RegistrationManager) -> bool:
        stop_requested = False
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                if command.operation == "configure":
                    manager.apply(command.payload)
                    command.result = manager.snapshot()
                    with self._state_lock:
                        self._snapshot = dict(command.result)
                elif command.operation == "stop":
                    # Exit the pump even if native cleanup reports an error;
                    # the thread-level finally block makes one more cleanup pass.
                    stop_requested = True
                    manager.clear()
                    with self._state_lock:
                        self._snapshot = {}
                    command.result = None
                else:
                    raise HotkeyError(f"unknown hotkey command: {command.operation}")
            except BaseException as error:
                command.error = error
                if command.operation == "configure":
                    with self._state_lock:
                        self._snapshot = manager.snapshot()
            finally:
                if command.done is not None:
                    command.done.set()
        return stop_requested

    def _run_callback(self, callback: Callable[[], object]) -> None:
        try:
            callback()
        except Exception as error:
            if self._on_callback_error is not None:
                try:
                    self._on_callback_error(error)
                except Exception:
                    pass

    def _fail_pending_commands(self, error: BaseException) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.error = error
            if command.done is not None:
                command.done.set()

    def __enter__(self) -> "NativeHotkeyService":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()


__all__ = [
    "HotkeyBinding",
    "HotkeyError",
    "HotkeyParseError",
    "HotkeyRegistrationError",
    "HotkeyUnavailableError",
    "KNOWN_HOTKEY_CONFLICTS",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "MOD_WIN",
    "NativeHotkeyService",
    "ParsedHotkey",
    "PreparedHotkeyBinding",
    "describe_hotkey_conflict_risk",
    "parse_hotkey",
    "prepare_bindings",
]
