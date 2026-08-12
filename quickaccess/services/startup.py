"""Manage QuickAccess's per-user Windows startup registry entry."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

try:  # Importing the module must remain safe on non-Windows hosts.
    import winreg as _winreg
except ImportError:  # pragma: no cover - exercised on non-Windows CI
    _winreg = None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_VALUE_NAME = "QuickAccessLauncher"


class StartupUnavailableError(RuntimeError):
    """Raised when the HKCU Run key cannot be used on this platform."""


def build_startup_command(
    executable: str | os.PathLike[str],
    arguments: Sequence[str | os.PathLike[str]] = (),
) -> str:
    """Build a command line using Windows' CommandLineToArgvW quoting rules."""

    executable_text = os.fspath(executable)
    if not executable_text or "\x00" in executable_text:
        raise ValueError("executable must be a non-empty path without NUL characters")
    parts = [executable_text]
    for argument in arguments:
        argument_text = os.fspath(argument)
        if "\x00" in argument_text:
            raise ValueError("startup arguments must not contain NUL characters")
        parts.append(argument_text)
    return subprocess.list2cmdline(parts)


class StartupManager:
    """Own only this application's value under the current user's Run key."""

    def __init__(
        self,
        value_name: str = DEFAULT_VALUE_NAME,
        registry: object | None = None,
    ) -> None:
        if not value_name or "\x00" in value_name:
            raise ValueError("value_name must be non-empty and must not contain NUL")
        self.value_name = value_name
        self._registry = registry if registry is not None else _winreg

    @property
    def available(self) -> bool:
        return self._registry is not None and sys.platform == "win32"

    def _require_registry(self) -> object:
        if self._registry is None:
            raise StartupUnavailableError("the Windows registry is unavailable")
        return self._registry

    def get_command(self) -> str | None:
        registry = self._require_registry()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                registry.KEY_READ,
            ) as key:
                value, _value_type = registry.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            return None
        return str(value)

    def is_enabled(
        self,
        executable: str | os.PathLike[str] | None = None,
        arguments: Sequence[str | os.PathLike[str]] = (),
    ) -> bool:
        command = self.get_command()
        if command is None:
            return False
        if executable is None:
            return True
        expected = build_startup_command(executable, arguments)
        return command.casefold() == expected.casefold()

    def enable(
        self,
        executable: str | os.PathLike[str],
        arguments: Sequence[str | os.PathLike[str]] = (),
    ) -> str:
        registry = self._require_registry()
        command = build_startup_command(executable, arguments)
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            registry.KEY_SET_VALUE,
        ) as key:
            registry.SetValueEx(key, self.value_name, 0, registry.REG_SZ, command)
        return command

    def disable(self) -> bool:
        registry = self._require_registry()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                registry.KEY_SET_VALUE,
            ) as key:
                try:
                    registry.DeleteValue(key, self.value_name)
                except FileNotFoundError:
                    return False
        except FileNotFoundError:
            return False
        return True

    def set_enabled(
        self,
        enabled: bool,
        executable: str | os.PathLike[str],
        arguments: Sequence[str | os.PathLike[str]] = (),
    ) -> str | None:
        if enabled:
            return self.enable(executable, arguments)
        self.disable()
        return None


__all__ = [
    "DEFAULT_VALUE_NAME",
    "RUN_KEY",
    "StartupManager",
    "StartupUnavailableError",
    "build_startup_command",
]
