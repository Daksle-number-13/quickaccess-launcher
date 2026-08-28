"""Privacy-conscious diagnostics that can be copied from the settings UI."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import platform
import struct
import sys

from . import __author__, __version__
from .models import LauncherConfig


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    app_version: str
    author: str
    operating_system: str
    architecture: str
    python_version: str
    packaged: bool
    item_count: int
    folder_count: int
    file_count: int
    url_count: int
    appearance_mode: str
    columns: int
    panel_hotkey: str
    quick_add_hotkey: str
    startup_preference: bool
    startup_state: str
    update_checks: bool

    def render(self) -> str:
        """Return a stable report without launcher target paths or usernames."""

        return "\n".join(
            (
                "QuickAccess 진단 정보",
                f"버전: {self.app_version}",
                f"만든 사람: {self.author}",
                f"운영체제: {self.operating_system} ({self.architecture})",
                f"실행 환경: {'배포 EXE' if self.packaged else 'Python 개발 환경'}",
                f"Python: {self.python_version}",
                (
                    "바로가기: "
                    f"{self.item_count}개 "
                    f"(폴더 {self.folder_count}, 파일 {self.file_count}, 링크 {self.url_count})"
                ),
                f"화면: {self.appearance_mode}, {self.columns}열",
                f"패널 단축키: {self.panel_hotkey}",
                f"빠른 등록 단축키: {self.quick_add_hotkey}",
                (
                    "자동 실행: "
                    f"설정 {'켬' if self.startup_preference else '끔'}, "
                    f"실제 상태 {self.startup_state}"
                ),
                f"업데이트 확인: {'켬' if self.update_checks else '끔'}",
                "개인 파일 경로와 등록된 URL은 포함하지 않았습니다.",
            )
        )


def collect_diagnostics(
    config: LauncherConfig,
    *,
    startup_status: object | None = None,
) -> DiagnosticSnapshot:
    """Collect an in-memory snapshot without filesystem or network access."""

    if not isinstance(config, LauncherConfig):
        raise TypeError("config must be LauncherConfig")
    counts = Counter(item.type for item in config.items)
    state = getattr(startup_status, "state", None)
    startup_state = getattr(state, "value", None) or (
        "확인 안 됨" if state is None else str(state)
    )
    operating_system = " ".join(
        part
        for part in (platform.system(), platform.release(), platform.version())
        if part
    )
    return DiagnosticSnapshot(
        app_version=__version__,
        author=__author__,
        operating_system=operating_system or sys.platform,
        architecture=f"{platform.machine() or 'unknown'} / {struct.calcsize('P') * 8}-bit",
        python_version=platform.python_version(),
        packaged=bool(getattr(sys, "frozen", False)),
        item_count=len(config.items),
        folder_count=counts["folder"],
        file_count=counts["file"],
        url_count=counts["url"],
        appearance_mode=config.appearance_mode,
        columns=config.columns,
        panel_hotkey=config.hotkey,
        quick_add_hotkey=config.quick_add_hotkey,
        startup_preference=config.run_on_startup,
        startup_state=startup_state,
        update_checks=config.check_updates,
    )


__all__ = ["DiagnosticSnapshot", "collect_diagnostics"]
