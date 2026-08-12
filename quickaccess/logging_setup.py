"""Application logging and uncaught-exception capture."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import TracebackType
from typing import Callable, Mapping


APP_DIRECTORY_NAME = "QuickAccess"
LOG_FILENAME = "quickaccess.log"
_HANDLER_MARKER = "_quickaccess_rotating_handler"
_LOGGING_LOCK = threading.RLock()
_HOOK_LOCK = threading.RLock()


def get_log_directory(
    *, environ: Mapping[str, str] | None = None, app_name: str = APP_DIRECTORY_NAME
) -> Path:
    """Return a per-user log directory without creating it.

    ``LOCALAPPDATA`` is preferred because diagnostic logs should not roam with
    the user's profile.  ``APPDATA`` and a user-local fallback keep startup
    resilient on unusual or partially configured Windows profiles.
    """

    environment = os.environ if environ is None else environ
    base = environment.get("LOCALAPPDATA") or environment.get("APPDATA")
    if base:
        return Path(base) / app_name / "logs"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / app_name / "logs"
    return Path.home() / ".local" / "state" / app_name / "logs"


def configure_logging(
    *,
    log_directory: str | os.PathLike[str] | None = None,
    level: int = logging.INFO,
    max_bytes: int = 1_048_576,
    backup_count: int = 3,
    logger: logging.Logger | None = None,
) -> Path:
    """Install one UTF-8 rotating file handler and return its file path."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("backup_count must be non-negative")

    target_logger = logging.getLogger() if logger is None else logger
    explicit_directory = log_directory is not None
    directory = (
        get_log_directory()
        if log_directory is None
        else Path(log_directory).expanduser()
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        if log_directory is not None:
            raise
        directory = Path(tempfile.gettempdir()) / APP_DIRECTORY_NAME / "logs"
        directory.mkdir(parents=True, exist_ok=True)

    log_path = (directory / LOG_FILENAME).resolve()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    with _LOGGING_LOCK:
        existing: RotatingFileHandler | None = None
        for handler in list(target_logger.handlers):
            if not getattr(handler, _HANDLER_MARKER, False):
                continue
            current_path = Path(handler.baseFilename).resolve()
            if current_path == log_path and isinstance(handler, RotatingFileHandler):
                existing = handler
                continue
            target_logger.removeHandler(handler)
            handler.close()

        if existing is None:
            try:
                existing = _new_rotating_handler(
                    log_path, max_bytes=max_bytes, backup_count=backup_count
                )
            except OSError:
                if explicit_directory:
                    raise
                # A redirected or locked-down AppData directory must not make
                # the resident application fail before its UI can report the
                # problem.  The user's temporary directory is the final log
                # fallback.
                fallback_directory = (
                    Path(tempfile.gettempdir()) / APP_DIRECTORY_NAME / "logs"
                )
                fallback_directory.mkdir(parents=True, exist_ok=True)
                log_path = (fallback_directory / LOG_FILENAME).resolve()
                existing = _new_rotating_handler(
                    log_path, max_bytes=max_bytes, backup_count=backup_count
                )
            setattr(existing, _HANDLER_MARKER, True)
            target_logger.addHandler(existing)
        else:
            existing.maxBytes = max_bytes
            existing.backupCount = backup_count

        existing.setLevel(level)
        existing.setFormatter(formatter)
        target_logger.setLevel(level)

    return log_path


def _new_rotating_handler(
    log_path: Path, *, max_bytes: int, backup_count: int
) -> RotatingFileHandler:
    # Opening immediately verifies that the chosen directory is writable.  A
    # delayed handler would report the failure only on the first log record,
    # after configure_logging() had already claimed success.
    return RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,
    )


def close_logging(*, logger: logging.Logger | None = None) -> None:
    """Close handlers installed by :func:`configure_logging`."""

    target_logger = logging.getLogger() if logger is None else logger
    with _LOGGING_LOCK:
        for handler in list(target_logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                target_logger.removeHandler(handler)
                handler.close()


@dataclass(slots=True)
class ExceptionHookHandle:
    """Restorable process and thread exception-hook installation."""

    previous_sys_hook: Callable[[type[BaseException], BaseException, TracebackType | None], None]
    previous_thread_hook: Callable[[threading.ExceptHookArgs], None]
    installed_sys_hook: Callable[[type[BaseException], BaseException, TracebackType | None], None]
    installed_thread_hook: Callable[[threading.ExceptHookArgs], None]
    _active: bool = field(default=True, init=False)

    @property
    def active(self) -> bool:
        return self._active

    def restore(self) -> None:
        """Restore hooks if this handle still owns them."""

        global _ACTIVE_HOOKS
        with _HOOK_LOCK:
            if not self._active:
                return
            if sys.excepthook is self.installed_sys_hook:
                sys.excepthook = self.previous_sys_hook
            if threading.excepthook is self.installed_thread_hook:
                threading.excepthook = self.previous_thread_hook
            self._active = False
            if _ACTIVE_HOOKS is self:
                _ACTIVE_HOOKS = None


_ACTIVE_HOOKS: ExceptionHookHandle | None = None


def install_exception_hooks(
    *, logger: logging.Logger | None = None, chain: bool = True
) -> ExceptionHookHandle:
    """Log uncaught main-thread and worker-thread exceptions.

    Reinstalling replaces this module's previous hooks rather than stacking
    wrappers and duplicating log records.  Set ``chain=False`` for a windowed
    executable that has no useful stderr destination.
    """

    global _ACTIVE_HOOKS
    target_logger = logging.getLogger("quickaccess.crash") if logger is None else logger

    with _HOOK_LOCK:
        if _ACTIVE_HOOKS is not None and _ACTIVE_HOOKS.active:
            _ACTIVE_HOOKS.restore()

        previous_sys_hook = sys.excepthook
        previous_thread_hook = threading.excepthook

        def process_exception_hook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            traceback: TracebackType | None,
        ) -> None:
            if issubclass(exc_type, KeyboardInterrupt):
                if chain:
                    _call_safely(previous_sys_hook, exc_type, exc_value, traceback)
                return
            target_logger.critical(
                "Unhandled exception in main thread",
                exc_info=(exc_type, exc_value, traceback),
            )
            if chain:
                _call_safely(previous_sys_hook, exc_type, exc_value, traceback)

        def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
            thread_name = args.thread.name if args.thread is not None else "unknown"
            target_logger.critical(
                "Unhandled exception in thread %s",
                thread_name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            if chain:
                _call_safely(previous_thread_hook, args)

        handle = ExceptionHookHandle(
            previous_sys_hook=previous_sys_hook,
            previous_thread_hook=previous_thread_hook,
            installed_sys_hook=process_exception_hook,
            installed_thread_hook=thread_exception_hook,
        )
        sys.excepthook = process_exception_hook
        threading.excepthook = thread_exception_hook
        _ACTIVE_HOOKS = handle
        return handle


def _call_safely(function: Callable[..., None], *args: object) -> None:
    try:
        function(*args)
    except Exception:
        # In PyInstaller windowed mode the default hook can fail because stderr
        # is unavailable.  The rotating log already contains the exception.
        pass
