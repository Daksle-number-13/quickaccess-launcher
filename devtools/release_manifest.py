"""Create deterministic metadata beside a QuickAccess release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


MANIFEST_SCHEMA_VERSION = 2
ALLOWED_SIGNATURE_STATES = {
    "Valid",
    "NotSigned",
    "HashMismatch",
    "NotTrusted",
    "NotSupportedFileFormat",
    "Incompatible",
    "UnknownError",
}


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def create_release_manifest(
    artifact: Path,
    *,
    version: str,
    signature_status: str,
    python_version: str,
    pyinstaller_version: str,
    source_date_epoch: int,
    source_commit: str,
    source_dirty: bool,
    release_eligible: bool,
    license_bundle: Path,
) -> dict[str, object]:
    """Return stable, privacy-safe facts about one release executable."""

    artifact = artifact.resolve(strict=True)
    if artifact.name != "QuickAccess.exe":
        raise ValueError("release artifact must be named QuickAccess.exe")
    if signature_status not in ALLOWED_SIGNATURE_STATES:
        raise ValueError(f"unsupported Authenticode status: {signature_status}")
    if source_date_epoch < 0:
        raise ValueError("source date epoch must not be negative")
    normalized_commit = source_commit.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized_commit) is None:
        raise ValueError("source commit must be a hexadecimal Git object id")
    if release_eligible and source_dirty:
        raise ValueError("a dirty source tree cannot be release eligible")
    license_bundle = license_bundle.resolve(strict=True)
    expected_license_name = f"QuickAccess_Third_Party_Licenses_v{version}.zip"
    if license_bundle.name != expected_license_name:
        raise ValueError("unexpected third-party license bundle name")

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "QuickAccess Launcher",
        "version": version,
        "artifact": {
            "name": artifact.name,
            "size": artifact.stat().st_size,
            "sha256": artifact_sha256(artifact),
        },
        "authenticode": {
            "status": signature_status,
            "signed": signature_status == "Valid",
        },
        "build": {
            "python": python_version,
            "pyinstaller": pyinstaller_version,
            "source_date_epoch": source_date_epoch,
            "source_commit": normalized_commit,
            "source_dirty": bool(source_dirty),
            "release_eligible": bool(release_eligible),
        },
        "companion_artifacts": [
            {
                "name": license_bundle.name,
                "size": license_bundle.stat().st_size,
                "sha256": artifact_sha256(license_bundle),
            }
        ],
    }


def write_release_manifest(
    artifact: Path,
    output: Path,
    *,
    version: str,
    signature_status: str,
    python_version: str,
    pyinstaller_version: str,
    source_date_epoch: int,
    source_commit: str,
    source_dirty: bool,
    release_eligible: bool,
    license_bundle: Path,
) -> dict[str, object]:
    manifest = create_release_manifest(
        artifact,
        version=version,
        signature_status=signature_status,
        python_version=python_version,
        pyinstaller_version=pyinstaller_version,
        source_date_epoch=source_date_epoch,
        source_commit=source_commit,
        source_dirty=source_dirty,
        release_eligible=release_eligible,
        license_bundle=license_bundle,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signature-status", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--pyinstaller-version", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-dirty", required=True, choices=("true", "false"))
    parser.add_argument("--release-eligible", required=True, choices=("true", "false"))
    parser.add_argument("--license-bundle", required=True, type=Path)
    args = parser.parse_args()
    manifest = write_release_manifest(
        args.artifact,
        args.output,
        version=args.version,
        signature_status=args.signature_status,
        python_version=args.python_version,
        pyinstaller_version=args.pyinstaller_version,
        source_date_epoch=args.source_date_epoch,
        source_commit=args.source_commit,
        source_dirty=args.source_dirty == "true",
        release_eligible=args.release_eligible == "true",
        license_bundle=args.license_bundle,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
