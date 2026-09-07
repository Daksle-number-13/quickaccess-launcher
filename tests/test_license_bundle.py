from __future__ import annotations

from pathlib import Path
import shutil
import uuid
import zipfile

import pytest

from devtools import build_license_bundle


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def license_tmp() -> Path:
    path = ROOT / ".test-work" / f"license-bundle-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_license_bundle_is_deterministic_and_contains_notice(
    license_tmp: Path,
    monkeypatch,
) -> None:
    entries = {
        "QuickAccess-LICENSE.txt": b"MIT\n",
        "THIRD_PARTY_NOTICES.md": b"notices\n",
        "Dependency-LICENSE.txt": b"dependency\n",
    }
    monkeypatch.setattr(build_license_bundle, "collect_license_entries", lambda _root: entries)
    output = license_tmp / "licenses.zip"

    build_license_bundle.write_license_bundle(
        output,
        version="2.0.2",
        source_date_epoch=1_700_000_000,
        root=license_tmp,
    )
    first = output.read_bytes()
    build_license_bundle.write_license_bundle(
        output,
        version="2.0.2",
        source_date_epoch=1_700_000_000,
        root=license_tmp,
    )

    assert output.read_bytes() == first
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "QuickAccess-2.0.2-licenses/Dependency-LICENSE.txt",
            "QuickAccess-2.0.2-licenses/QuickAccess-LICENSE.txt",
            "QuickAccess-2.0.2-licenses/THIRD_PARTY_NOTICES.md",
        ]


def test_license_bundle_rejects_invalid_version(license_tmp: Path) -> None:
    try:
        build_license_bundle.write_license_bundle(
            license_tmp / "licenses.zip",
            version="v2",
            source_date_epoch=0,
            root=license_tmp,
        )
    except ValueError as error:
        assert "MAJOR.MINOR.PATCH" in str(error)
    else:
        raise AssertionError("invalid version was accepted")


def test_every_locked_distribution_exposes_license_text() -> None:
    pins = build_license_bundle.locked_requirements(ROOT / "requirements-dev.txt")
    missing: list[str] = []
    for display_name, _version in pins.values():
        try:
            entries = build_license_bundle._license_files_for_distribution(display_name)
        except (FileNotFoundError, build_license_bundle.importlib.metadata.PackageNotFoundError):
            missing.append(display_name)
            continue
        if not entries:
            missing.append(display_name)
    assert missing == []
