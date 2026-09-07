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
    license_bundle = release_tmp / "QuickAccess_Third_Party_Licenses_v2.0.2.zip"
    license_bundle.write_bytes(b"license bundle")

    manifest = create_release_manifest(
        artifact,
        version="2.0.2",
        signature_status="NotSigned",
        python_version="3.13.15",
        pyinstaller_version="6.19.0",
        source_date_epoch=1234567890,
        source_commit="0123456789abcdef0123456789abcdef01234567",
        source_dirty=False,
        release_eligible=True,
        license_bundle=license_bundle,
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
        "source_commit": "0123456789abcdef0123456789abcdef01234567",
        "source_dirty": False,
        "release_eligible": True,
    }
    assert manifest["companion_artifacts"] == [
        {
            "name": license_bundle.name,
            "size": license_bundle.stat().st_size,
            "sha256": artifact_sha256(license_bundle),
        }
    ]


def test_valid_signature_is_the_only_state_marked_signed(release_tmp: Path) -> None:
    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"signed placeholder")
    license_bundle = release_tmp / "QuickAccess_Third_Party_Licenses_v2.0.2.zip"
    license_bundle.write_bytes(b"licenses")
    manifest = create_release_manifest(
        artifact,
        version="2.0.2",
        signature_status="Valid",
        python_version="3.13.15",
        pyinstaller_version="6.19.0",
        source_date_epoch=0,
        source_commit="abcdef0123456789abcdef0123456789abcdef01",
        source_dirty=False,
        release_eligible=True,
        license_bundle=license_bundle,
    )
    assert manifest["authenticode"] == {"signed": True, "status": "Valid"}


def test_manifest_output_is_stable_and_atomic(release_tmp: Path) -> None:
    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"same bytes")
    license_bundle = release_tmp / "QuickAccess_Third_Party_Licenses_v2.0.2.zip"
    license_bundle.write_bytes(b"licenses")
    output = release_tmp / "QuickAccess.release.json"
    kwargs = {
        "version": "2.0.2",
        "signature_status": "NotSigned",
        "python_version": "3.13.15",
        "pyinstaller_version": "6.19.0",
        "source_date_epoch": 42,
        "source_commit": "0123456789abcdef0123456789abcdef01234567",
        "source_dirty": False,
        "release_eligible": True,
        "license_bundle": license_bundle,
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
    license_bundle = release_tmp / "QuickAccess_Third_Party_Licenses_v2.0.2.zip"
    license_bundle.write_bytes(b"licenses")
    common = {
        "version": "2.0.2",
        "python_version": "3.13.15",
        "pyinstaller_version": "6.19.0",
        "source_date_epoch": 0,
        "source_commit": "0123456789abcdef0123456789abcdef01234567",
        "source_dirty": False,
        "release_eligible": True,
        "license_bundle": license_bundle,
    }
    with pytest.raises(ValueError, match="QuickAccess.exe"):
        create_release_manifest(wrong_name, signature_status="NotSigned", **common)

    artifact = release_tmp / "QuickAccess.exe"
    artifact.write_bytes(b"x")
    with pytest.raises(ValueError, match="Authenticode"):
        create_release_manifest(artifact, signature_status="Unverified", **common)

    with pytest.raises(ValueError, match="dirty source"):
        create_release_manifest(
            artifact,
            signature_status="NotSigned",
            **{**common, "source_dirty": True},
        )
