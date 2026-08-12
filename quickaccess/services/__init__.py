"""Platform and background services used by the QuickAccess controller."""

from .explorer import (
    ExplorerErrorCode,
    ExplorerQuickAddService,
    ExplorerTargetResult,
    ExplorerTargetSource,
    get_foreground_window,
)
from .hotkeys import (
    HotkeyBinding,
    HotkeyError,
    HotkeyParseError,
    HotkeyRegistrationError,
    HotkeyUnavailableError,
    NativeHotkeyService,
    ParsedHotkey,
    parse_hotkey,
    prepare_bindings,
)
from .launcher import FileLauncher, LaunchErrorCode, LaunchResult, launch_path
from .monitor import (
    MonitorUnavailableError,
    NativeMonitorService,
    Point,
    Rect,
    Size,
    clamp_window_to_work_area,
)
from .singleton import SingleInstanceGuard, SingletonUnavailableError, local_mutex_name
from .startup import StartupManager, StartupUnavailableError, build_startup_command
from .validation import PathStatus, PathValidationService, ValidationResult

__all__ = [
    "ExplorerErrorCode",
    "ExplorerQuickAddService",
    "ExplorerTargetResult",
    "ExplorerTargetSource",
    "FileLauncher",
    "HotkeyBinding",
    "HotkeyError",
    "HotkeyParseError",
    "HotkeyRegistrationError",
    "HotkeyUnavailableError",
    "LaunchErrorCode",
    "LaunchResult",
    "MonitorUnavailableError",
    "NativeHotkeyService",
    "NativeMonitorService",
    "ParsedHotkey",
    "PathStatus",
    "PathValidationService",
    "Point",
    "Rect",
    "SingleInstanceGuard",
    "SingletonUnavailableError",
    "Size",
    "StartupManager",
    "StartupUnavailableError",
    "ValidationResult",
    "build_startup_command",
    "clamp_window_to_work_area",
    "get_foreground_window",
    "launch_path",
    "local_mutex_name",
    "parse_hotkey",
    "prepare_bindings",
]
