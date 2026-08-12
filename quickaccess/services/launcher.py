"""Exception-safe wrapper for opening files and folders with Windows Shell."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class LaunchErrorCode(str, Enum):
    INVALID_PATH = "invalid_path"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LaunchResult:
    path: str
    success: bool
    error_code: LaunchErrorCode | None = None
    error: str | None = None


class FileLauncher:
    """Call ``os.startfile`` without allowing failures to escape a UI callback."""

    def __init__(self, startfile: Callable[[str], object] | None = None) -> None:
        self._startfile = startfile if startfile is not None else getattr(os, "startfile", None)

    def launch(self, path: str | os.PathLike[str]) -> LaunchResult:
        try:
            path_text = os.fspath(path)
        except Exception as error:
            return LaunchResult("", False, LaunchErrorCode.INVALID_PATH, str(error))

        if not isinstance(path_text, str):
            return LaunchResult(
                "",
                False,
                LaunchErrorCode.INVALID_PATH,
                "path must resolve to text",
            )
        if not path_text or "\x00" in path_text:
            return LaunchResult(
                path_text,
                False,
                LaunchErrorCode.INVALID_PATH,
                "path must be non-empty and must not contain NUL",
            )
        if self._startfile is None:
            return LaunchResult(
                path_text,
                False,
                LaunchErrorCode.UNAVAILABLE,
                "os.startfile is available only on Windows",
            )
        try:
            self._startfile(path_text)
        except Exception as error:  # External shell errors must not kill the resident app.
            return LaunchResult(path_text, False, LaunchErrorCode.FAILED, str(error))
        return LaunchResult(path_text, True)


def launch_path(
    path: str | os.PathLike[str],
    startfile: Callable[[str], object] | None = None,
) -> LaunchResult:
    return FileLauncher(startfile=startfile).launch(path)


__all__ = ["FileLauncher", "LaunchErrorCode", "LaunchResult", "launch_path"]
