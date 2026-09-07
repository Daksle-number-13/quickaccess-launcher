from __future__ import annotations

from pathlib import Path
import shutil
import uuid

import pytest

from devtools import check_locked_environment


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lock_tmp() -> Path:
    path = ROOT / ".test-work" / f"environment-lock-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_lock_reader_follows_includes_and_markers(lock_tmp: Path) -> None:
    (lock_tmp / "runtime.txt").write_text(
        'Pillow==12.3.0\npywin32==311; sys_platform == "win32"\n',
        encoding="utf-8",
    )
    root = lock_tmp / "dev.txt"
    root.write_text("-r runtime.txt\npytest==9.0.2\n", encoding="utf-8")

    pins = check_locked_environment.locked_requirements(
        root,
        environment={"sys_platform": "linux", "extra": ""},
    )

    assert pins == {
        "pillow": ("Pillow", "12.3.0"),
        "pytest": ("pytest", "9.0.2"),
    }


def test_lock_reader_rejects_ranges(lock_tmp: Path) -> None:
    requirements = lock_tmp / "requirements.txt"
    requirements.write_text("pytest>=9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact version pin"):
        check_locked_environment.locked_requirements(requirements)


def test_environment_check_reports_missing_and_drift(
    lock_tmp: Path,
    monkeypatch,
) -> None:
    requirements = lock_tmp / "requirements.txt"
    requirements.write_text("alpha==1.0\nbeta==2.0\n", encoding="utf-8")

    def version(name: str) -> str:
        if name == "alpha":
            return "9.0"
        raise check_locked_environment.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(check_locked_environment.metadata, "version", version)
    with pytest.raises(RuntimeError) as raised:
        check_locked_environment.check_locked_environment(requirements)
    assert "alpha is 9.0, expected 1.0" in str(raised.value)
    assert "beta is not installed" in str(raised.value)
