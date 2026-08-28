"""Manage QuickAccess's per-user Windows startup registry entry."""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

try:  # Importing the module must remain safe on non-Windows hosts.
    import winreg as _winreg
except ImportError:  # pragma: no cover - exercised on non-Windows CI
    _winreg = None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_VALUE_NAME = "QuickAccessLauncher"


class StartupUnavailableError(RuntimeError):
    """Raised when the HKCU Run key cannot be used on this platform."""


class StartupRegistrationState(str, Enum):
    """The authoritative state of QuickAccess's HKCU Run value."""

    ABSENT = "absent"
    CORRECT = "correct"
    STALE = "stale"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class StartupRegistrationStatus:
    """A registry snapshot kept separate from the persisted user preference.

    ``state`` describes the value that is actually present in HKCU.  The
    ``desired_enabled`` preference is deliberately retained alongside it so a
    caller can surface and repair drift instead of assuming that saving JSON
    also changed Windows.
    """

    state: StartupRegistrationState
    desired_enabled: bool
    expected_command: str
    actual_command: str | None = None
    error: str | None = None

    @property
    def registered(self) -> bool:
        """Whether a readable value currently exists, even if it is stale."""

        return self.state in {
            StartupRegistrationState.CORRECT,
            StartupRegistrationState.STALE,
        }

    @property
    def in_sync(self) -> bool:
        """Whether the registry matches the persisted desired state."""

        if self.state is StartupRegistrationState.UNREADABLE:
            return False
        if self.desired_enabled:
            return self.state is StartupRegistrationState.CORRECT
        return self.state is StartupRegistrationState.ABSENT

    @property
    def repairable(self) -> bool:
        """Whether reconciliation can make a meaningful registry change."""

        return not self.in_sync


_PERCENT_ENVIRONMENT_VARIABLE = re.compile(r"%([^%]+)%")


def _expand_windows_environment_variables(
    value: str,
    environment: Mapping[str, str],
) -> str:
    """Expand Windows ``%NAME%`` variables case-insensitively and in memory."""

    normalized_environment = {
        str(name).casefold(): str(replacement)
        for name, replacement in environment.items()
    }

    def replace(match: re.Match[str]) -> str:
        return normalized_environment.get(match.group(1).casefold(), match.group(0))

    return _PERCENT_ENVIRONMENT_VARIABLE.sub(replace, value)


def _normalize_windows_executable(
    value: str,
    environment: Mapping[str, str],
) -> str:
    expanded = _expand_windows_environment_variables(value, environment)
    # The extended-length spelling is equivalent to the ordinary spelling for
    # a local absolute path. Normalizing it avoids a false stale result after
    # installers or launchers rewrite the Run value.
    if expanded.casefold().startswith("\\\\?\\unc\\"):
        expanded = r"\\" + expanded[8:]
    elif expanded.casefold().startswith("\\\\?\\"):
        expanded = expanded[4:]
    return ntpath.normcase(ntpath.normpath(expanded))


def _split_windows_command_line(command: str) -> tuple[str, ...]:
    """Parse a Windows command line using CommandLineToArgvW-style rules.

    This pure implementation keeps startup inspection deterministic on CI and
    avoids filesystem, subprocess, or UI-thread work. It mirrors the quote and
    backslash rules used by :func:`subprocess.list2cmdline`.
    """

    parsed: list[str] = []
    length = len(command)
    index = 0
    whitespace = " \t"

    while True:
        while index < length and command[index] in whitespace:
            index += 1
        if index >= length:
            return tuple(parsed)

        argument: list[str] = []
        in_quotes = False
        while index < length:
            character = command[index]
            if character in whitespace and not in_quotes:
                break

            if character == "\\":
                slash_start = index
                while index < length and command[index] == "\\":
                    index += 1
                slash_count = index - slash_start
                if index < length and command[index] == '"':
                    argument.extend("\\" for _ in range(slash_count // 2))
                    if slash_count % 2:
                        argument.append('"')
                        index += 1
                    elif in_quotes and index + 1 < length and command[index + 1] == '"':
                        argument.append('"')
                        index += 2
                    else:
                        in_quotes = not in_quotes
                        index += 1
                else:
                    argument.extend("\\" for _ in range(slash_count))
                continue

            if character == '"':
                if in_quotes and index + 1 < length and command[index + 1] == '"':
                    argument.append('"')
                    index += 2
                else:
                    in_quotes = not in_quotes
                    index += 1
                continue

            argument.append(character)
            index += 1

        parsed.append("".join(argument))
        while index < length and command[index] in whitespace:
            index += 1


def startup_command_matches(
    actual_command: str,
    executable: str | os.PathLike[str],
    arguments: Sequence[str | os.PathLike[str]] = (),
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a Run value invokes exactly the expected startup entry.

    Executable paths use Windows' case-insensitive path semantics while
    arguments remain exact because changing an option can change application
    behaviour. Equivalent quoting, path separators, ``.`` components,
    extended-length prefixes, and ``%ENVIRONMENT%`` path spellings compare
    equal without touching disk.
    """

    if not isinstance(actual_command, str) or not actual_command.strip():
        return False
    expected_executable = os.fspath(executable)
    if not expected_executable or "\x00" in expected_executable:
        raise ValueError("executable must be a non-empty path without NUL characters")

    expected_arguments = tuple(os.fspath(argument) for argument in arguments)
    if any("\x00" in argument for argument in expected_arguments):
        raise ValueError("startup arguments must not contain NUL characters")

    actual_parts = _split_windows_command_line(actual_command)
    if len(actual_parts) != len(expected_arguments) + 1:
        return False

    active_environment = os.environ if environment is None else environment
    if _normalize_windows_executable(actual_parts[0], active_environment) != (
        _normalize_windows_executable(expected_executable, active_environment)
    ):
        return False
    return actual_parts[1:] == expected_arguments


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
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not value_name or "\x00" in value_name:
            raise ValueError("value_name must be non-empty and must not contain NUL")
        self.value_name = value_name
        self._registry = registry if registry is not None else _winreg
        self._environment = os.environ if environment is None else environment

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
        return startup_command_matches(
            command,
            executable,
            arguments,
            environment=self._environment,
        )

    def inspect(
        self,
        desired_enabled: bool,
        executable: str | os.PathLike[str],
        arguments: Sequence[str | os.PathLike[str]] = (),
    ) -> StartupRegistrationStatus:
        """Inspect actual HKCU state without mutating it or raising read errors."""

        expected = build_startup_command(executable, arguments)
        registry = self._registry
        if registry is None:
            return StartupRegistrationStatus(
                state=StartupRegistrationState.UNREADABLE,
                desired_enabled=bool(desired_enabled),
                expected_command=expected,
                error="the Windows registry is unavailable",
            )

        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                registry.KEY_READ,
            ) as key:
                raw_value, value_type = registry.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            return StartupRegistrationStatus(
                state=StartupRegistrationState.ABSENT,
                desired_enabled=bool(desired_enabled),
                expected_command=expected,
            )
        except Exception as error:
            return StartupRegistrationStatus(
                state=StartupRegistrationState.UNREADABLE,
                desired_enabled=bool(desired_enabled),
                expected_command=expected,
                error=f"{type(error).__name__}: {error}",
            )

        actual = raw_value if isinstance(raw_value, str) else str(raw_value)
        supported_types = {registry.REG_SZ}
        expand_string_type = getattr(registry, "REG_EXPAND_SZ", None)
        if expand_string_type is not None:
            supported_types.add(expand_string_type)
        matches = value_type in supported_types and startup_command_matches(
            actual,
            executable,
            arguments,
            environment=self._environment,
        )
        return StartupRegistrationStatus(
            state=(
                StartupRegistrationState.CORRECT
                if matches
                else StartupRegistrationState.STALE
            ),
            desired_enabled=bool(desired_enabled),
            expected_command=expected,
            actual_command=actual,
        )

    def reconcile(
        self,
        desired_enabled: bool,
        executable: str | os.PathLike[str],
        arguments: Sequence[str | os.PathLike[str]] = (),
    ) -> StartupRegistrationStatus:
        """Repair registry drift and return a post-write authoritative snapshot.

        Registry write errors intentionally propagate so existing controller
        rollback and user-notification behaviour remains intact.
        """

        current = self.inspect(desired_enabled, executable, arguments)
        if current.in_sync:
            return current
        if desired_enabled:
            self.enable(executable, arguments)
        else:
            self.disable()
        return self.inspect(desired_enabled, executable, arguments)

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
    "StartupRegistrationState",
    "StartupRegistrationStatus",
    "StartupUnavailableError",
    "build_startup_command",
    "startup_command_matches",
]
