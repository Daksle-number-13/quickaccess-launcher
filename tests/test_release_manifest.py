from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

import pytest

from devtools.release_manifest import (
    artifact_sha256,
    create_release_manifest,
    write_release_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_tmp() -> Path:
    path = ROOT / ".test-work" / f"release-manifest-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_release_manifest_records_hash_and_honest_unsigned_state(release_tmp: Path) -> None:
    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"portable executable placeholder")

    manifest = create_release_manifest(
        artifact,
        version="1.2.5",
        signature_status="NotSigned",
        python_version="3.13.15",
        pyinstaller_version="6.19.0",
        source_date_epoch=1234567890,
    )

    assert manifest["artifact"] == {
        "name": "QuickAccess.exe",
        "size": artifact.stat().st_size,
        "sha256": artifact_sha256(artifact),
    }
    assert manifest["authenticode"] == {"signed": False, "status": "NotSigned"}
    assert manifest["build"] == {
        "pyinstaller": "6.19.0",
        "python": "3.13.15",
        "source_date_epoch": 1234567890,
    }


def test_valid_signature_is_the_only_state_marked_signed(release_tmp: Path) -> None:
    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"signed placeholder")
    manifest = create_release_manifest(
        artifact,
        version="1.2.5",
        signature_status="Valid",
        python_version="3.13.15",
        pyinstaller_version="6.19.0",
        source_date_epoch=0,
    )
    assert manifest["authenticode"] == {"signed": True, "status": "Valid"}


def test_manifest_output_is_stable_and_atomic(release_tmp: Path) -> None:
    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"same bytes")
    output = release_tmp / "QuickAccess.release.json"
    kwargs = {
        "version": "1.2.5",
        "signature_status": "NotSigned",
        "python_version": "3.13.15",
        "pyinstaller_version": "6.19.0",
        "source_date_epoch": 42,
    }
    write_release_manifest(artifact, output, **kwargs)
    first = output.read_bytes()
    write_release_manifest(artifact, output, **kwargs)
    assert output.read_bytes() == first
    assert json.loads(first)["artifact"]["sha256"] == artifact_sha256(artifact)
    assert not (release_tmp / ".QuickAccess.release.json.tmp").exists()


def test_manifest_rejects_wrong_artifact_name_and_unknown_signature(
    release_tmp: Path,
) -> None:
    wrong_name = release_tmp / "renamed.exe"
    wrong_name.write_bytes(b"x")
    common = {
        "version": "1.2.5",
        "python_version": "3.13.15",
        "pyinstaller_version": "6.19.0",
        "source_date_epoch": 0,
    }
    with pytest.raises(ValueError, match="QuickAccess.exe"):
        create_release_manifest(wrong_name, signature_status="NotSigned", **common)

    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"x")
    with pytest.raises(ValueError, match="Authenticode"):
        create_release_manifest(artifact, signature_status="Unverified", **common)
