"""Safely discover a quick-add target from the foreground Explorer window."""

from __future__ import annotations

import ctypes
import ntpath
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..models import detect_item_type


class ExplorerErrorCode(str, Enum):
    UNAVAILABLE = "unavailable"
    NO_FOREGROUND_EXPLORER = "no_foreground_explorer"
    NO_FILESYSTEM_PATH = "no_filesystem_path"
    COM_FAILURE = "com_failure"


class ExplorerTargetSource(str, Enum):
    SELECTION = "selection"
    CURRENT_FOLDER = "current_folder"


@dataclass(frozen=True, slots=True)
class ExplorerTargetResult:
    success: bool
    path: str | None = None
    suggested_name: str | None = None
    item_type: str | None = None
    source: ExplorerTargetSource | None = None
    hwnd: int | None = None
    error_code: ExplorerErrorCode | None = None
    error: str | None = None
    detail: str | None = None

    @classmethod
    def failure(
        cls,
        code: ExplorerErrorCode,
        error: str,
        *,
        hwnd: int | None = None,
        detail: str | None = None,
    ) -> "ExplorerTargetResult":
        return cls(False, hwnd=hwnd, error_code=code, error=error, detail=detail)


class _NoOpComRuntime:
    """Test helper protocol implementation; production uses pythoncom."""

    @staticmethod
    def CoInitialize() -> None:  # noqa: N802 - mirrors pythoncom
        return None

    @staticmethod
    def CoUninitialize() -> None:  # noqa: N802 - mirrors pythoncom
        return None


def _load_default_dependencies() -> tuple[object | None, Callable[[], object] | None]:
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        return None, None
    return pythoncom, lambda: win32com.client.Dispatch("Shell.Application")


def get_foreground_window() -> int:
    if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
        raise OSError("GetForegroundWindow is available only on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = user32.GetForegroundWindow()
    return int(hwnd or 0)


def _filesystem_path(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("::") or text.casefold().startswith("shell:"):
        return None
    # ntpath is intentional: these paths are Windows paths even when tests run
    # on another host.
    if not ntpath.isabs(text):
        return None
    return ntpath.normpath(text)


def _resolve_shortcut_target(selected_item: object, selected_path: str) -> str | None:
    """Resolve a selected ``.lnk`` shortcut to the file/folder it points at.

    ``FolderItem.Path`` for a shortcut is the ``.lnk`` file itself, not its
    target, so registering it without resolution would launch the shortcut
    icon instead of the destination the user actually meant to add.
    """

    if not selected_path.casefold().endswith(".lnk"):
        return None
    try:
        target = _filesystem_path(selected_item.GetLink.Path)
    except Exception:
        return None
    return target


def _suggested_name(path: str) -> str:
    stripped = path.rstrip("\\/")
    return ntpath.basename(stripped) or path


class ExplorerQuickAddService:
    """Resolve the first selected item, or current folder, in foreground Explorer.

    The public method never raises.  It initializes COM on the calling thread,
    so callers may safely invoke it from a background worker.
    """

    def __init__(
        self,
        *,
        shell_factory: Callable[[], object] | None = None,
        com_runtime: object | None = None,
        foreground_window: Callable[[], int] | None = None,
    ) -> None:
        self._shell_factory = shell_factory
        self._com_runtime = com_runtime
        self._needs_default_factory = shell_factory is None
        self._needs_default_runtime = com_runtime is None
        self._foreground_window = foreground_window or get_foreground_window

    def _ensure_dependencies(self) -> None:
        if not (self._needs_default_factory or self._needs_default_runtime):
            return
        default_runtime, default_factory = _load_default_dependencies()
        if self._needs_default_runtime:
            self._com_runtime = default_runtime
        if self._needs_default_factory:
            self._shell_factory = default_factory
        self._needs_default_runtime = False
        self._needs_default_factory = False

    @property
    def available(self) -> bool:
        self._ensure_dependencies()
        return self._shell_factory is not None and self._com_runtime is not None

    def capture_foreground_hwnd(self) -> int | None:
        """Capture the foreground HWND without letting a Win32 error escape."""

        try:
            hwnd = int(self._foreground_window() or 0)
        except Exception:
            return None
        return hwnd or None

    def get_target(self, foreground_hwnd: int | None = None) -> ExplorerTargetResult:
        """Resolve a target using a hotkey-time HWND when one was supplied."""

        if not self.available:
            return ExplorerTargetResult.failure(
                ExplorerErrorCode.UNAVAILABLE,
                "현재 열린 탐색기 창이 없습니다",
                detail="Shell.Application COM 지원을 사용할 수 없습니다",
            )

        initialized = False
        hwnd: int | None = None
        try:
            self._com_runtime.CoInitialize()
            initialized = True
            hwnd = (
                int(foreground_hwnd or 0)
                if foreground_hwnd is not None
                else int(self._foreground_window() or 0)
            )
            if not hwnd:
                return ExplorerTargetResult.failure(
                    ExplorerErrorCode.NO_FOREGROUND_EXPLORER,
                    "현재 열린 탐색기 창이 없습니다",
                )

            shell = self._shell_factory()
            explorer_window = self._find_window(shell, hwnd)
            if explorer_window is None:
                return ExplorerTargetResult.failure(
                    ExplorerErrorCode.NO_FOREGROUND_EXPLORER,
                    "현재 열린 탐색기 창이 없습니다",
                    hwnd=hwnd,
                )

            target, source, item_type = self._target_from_window(explorer_window)
            if target is None:
                return ExplorerTargetResult.failure(
                    ExplorerErrorCode.NO_FILESYSTEM_PATH,
                    "현재 탐색기 위치를 파일 경로로 변환할 수 없습니다",
                    hwnd=hwnd,
                )
            return ExplorerTargetResult(
                True,
                path=target,
                suggested_name=_suggested_name(target),
                item_type=item_type,
                source=source,
                hwnd=hwnd,
            )
        except Exception as error:  # COM/window races are expected external failures.
            return ExplorerTargetResult.failure(
                ExplorerErrorCode.COM_FAILURE,
                "현재 열린 탐색기 창이 없습니다",
                hwnd=hwnd,
                detail=str(error),
            )
        finally:
            if initialized:
                try:
                    self._com_runtime.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _find_window(shell: object, foreground_hwnd: int) -> object | None:
        windows = shell.Windows()
        try:
            iterator = iter(windows)
        except TypeError:
            iterator = (windows.Item(index) for index in range(int(windows.Count)))
        for window in iterator:
            try:
                if int(window.HWND) == foreground_hwnd:
                    return window
            except Exception:
                continue
        return None

    @staticmethod
    def _target_from_window(
        explorer_window: object,
    ) -> tuple[str | None, ExplorerTargetSource | None, str | None]:
        document = explorer_window.Document
        selected_items = document.SelectedItems()
        if int(selected_items.Count) > 0:
            selected_item = selected_items.Item(0)
            selected_path = _filesystem_path(selected_item.Path)
            if selected_path is None:
                return None, ExplorerTargetSource.SELECTION, None

            shortcut_target = _resolve_shortcut_target(selected_item, selected_path)
            if shortcut_target is not None:
                return shortcut_target, ExplorerTargetSource.SELECTION, detect_item_type(
                    shortcut_target
                )

            try:
                item_type = "folder" if bool(selected_item.IsFolder) else "file"
            except Exception:
                # COM FolderItem normally supplies IsFolder.  If a shell
                # extension does not, avoid a synchronous filesystem probe on
                # Tk's thread.  Treat the uncommon unknown selection as a file.
                item_type = "file"
            return selected_path, ExplorerTargetSource.SELECTION, item_type

        folder_path = _filesystem_path(document.Folder.Self.Path)
        if folder_path is not None:
            return folder_path, ExplorerTargetSource.CURRENT_FOLDER, "folder"
        return None, None, None


__all__ = [
    "ExplorerErrorCode",
    "ExplorerQuickAddService",
    "ExplorerTargetResult",
    "ExplorerTargetSource",
    "get_foreground_window",
]
