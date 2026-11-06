"""Companion files for a Rust QuickAccess release: license bundle and manifest.

Called by build-rust.ps1. Uses only the standard library and `cargo metadata`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devtools.release_manifest import (  # noqa: E402
    ALLOWED_SIGNATURE_STATES,
    MANIFEST_SCHEMA_VERSION,
    artifact_sha256,
)

TARGET = "x86_64-pc-windows-msvc"
_LICENSE_FILE = re.compile(r"^(?:licen[cs]e|copying|notice|copyright)", re.IGNORECASE)
_VERSION = re.compile(r"\d+\.\d+\.\d+")


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.") or "component"


def shipped_crates(cargo: str, manifest: Path) -> list[dict]:
    """Packages reachable from the app through normal (non-build, non-dev) edges.

    Proc-macro crates are included although their code does not ship, which
    errs toward giving more notices rather than fewer.
    """

    output = subprocess.run(
        [
            cargo, "metadata", "--format-version", "1", "--locked",
            "--filter-platform", TARGET, "--manifest-path", str(manifest),
        ],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    metadata = json.loads(output)
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    root = metadata["resolve"]["root"]
    seen: set[str] = set()
    pending = [root]
    while pending:
        for dependency in nodes[pending.pop()]["deps"]:
            normal = any(kind["kind"] is None for kind in dependency["dep_kinds"])
            if normal and dependency["pkg"] not in seen:
                seen.add(dependency["pkg"])
                pending.append(dependency["pkg"])
    return sorted((packages[i] for i in seen), key=lambda p: (p["name"], p["version"]))


def collect_license_entries(cargo: str, manifest: Path) -> dict[str, bytes]:
    entries: dict[str, bytes] = {
        "QuickAccess-LICENSE.txt": (ROOT / "LICENSE").read_bytes(),
        "THIRD_PARTY_NOTICES.md": (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes(),
    }
    lines = []
    for package in shipped_crates(cargo, manifest):
        name, version = package["name"], package["version"]
        expression = package.get("license") or "(see license files)"
        lines.append(f"{name} {version}\t{expression}")
        directory = Path(package["manifest_path"]).parent
        files = sorted(
            path for path in directory.iterdir()
            if path.is_file() and _LICENSE_FILE.match(path.name)
        )
        if not files:
            raise FileNotFoundError(f"no license files found for {name} {version}")
        for path in files:
            entries[f"crates/{_safe(name)}-{_safe(version)}/{_safe(path.name)}"] = path.read_bytes()
    entries["CRATE_VERSIONS.txt"] = ("\n".join(lines) + "\n").encode("utf-8")
    return entries


def write_license_bundle(output: Path, *, version: str, source_date_epoch: int, cargo: str, manifest: Path) -> None:
    if source_date_epoch < 0:
        raise ValueError("source date epoch must not be negative")
    if not _VERSION.fullmatch(version):
        raise ValueError("version must use numeric MAJOR.MINOR.PATCH form")
    if output.name != f"QuickAccess_Third_Party_Licenses_v{version}.zip":
        raise ValueError("unexpected third-party license bundle name")
    entries = collect_license_entries(cargo, manifest)
    timestamp = datetime.fromtimestamp(max(source_date_epoch, 315532800), timezone.utc).timetuple()[:6]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries, key=str.casefold):
            info = zipfile.ZipInfo(f"QuickAccess-{version}-licenses/{name}", timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    temporary.replace(output)


def write_manifest(
    artifact: Path,
    output: Path,
    *,
    version: str,
    signature_status: str,
    rust_version: str,
    source_date_epoch: int,
    source_commit: str,
    source_dirty: bool,
    release_eligible: bool,
    license_bundle: Path,
) -> dict:
    """Same schema as the Python releases, so sign.ps1 accepts it unchanged."""

    artifact = artifact.resolve(strict=True)
    license_bundle = license_bundle.resolve(strict=True)
    commit = source_commit.strip().lower()
    if artifact.name != "QuickAccess.exe":
        raise ValueError("release artifact must be named QuickAccess.exe")
    if signature_status not in ALLOWED_SIGNATURE_STATES:
        raise ValueError(f"unsupported Authenticode status: {signature_status}")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise ValueError("source commit must be a hexadecimal Git object id")
    if release_eligible and source_dirty:
        raise ValueError("a dirty source tree cannot be release eligible")
    if license_bundle.name != f"QuickAccess_Third_Party_Licenses_v{version}.zip":
        raise ValueError("unexpected third-party license bundle name")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "QuickAccess Launcher",
        "version": version,
        "artifact": {
            "name": artifact.name,
            "size": artifact.stat().st_size,
            "sha256": artifact_sha256(artifact),
        },
        "authenticode": {"status": signature_status, "signed": signature_status == "Valid"},
        "build": {
            "implementation": "rust",
            "rust": rust_version,
            "target": TARGET,
            "source_date_epoch": source_date_epoch,
            "source_commit": commit,
            "source_dirty": source_dirty,
            "release_eligible": release_eligible,
        },
        "companion_artifacts": [
            {
                "name": license_bundle.name,
                "size": license_bundle.stat().st_size,
                "sha256": artifact_sha256(license_bundle),
            }
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    temporary.replace(output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    licenses = commands.add_parser("licenses")
    licenses.add_argument("--output", required=True, type=Path)
    licenses.add_argument("--version", required=True)
    licenses.add_argument("--source-date-epoch", required=True, type=int)
    licenses.add_argument("--cargo", default="cargo")
    licenses.add_argument("--manifest-path", type=Path, default=ROOT / "rust" / "Cargo.toml")
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--artifact", required=True, type=Path)
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--signature-status", required=True)
    manifest.add_argument("--rust-version", required=True)
    manifest.add_argument("--source-date-epoch", required=True, type=int)
    manifest.add_argument("--source-commit", required=True)
    manifest.add_argument("--source-dirty", required=True, choices=("true", "false"))
    manifest.add_argument("--release-eligible", required=True, choices=("true", "false"))
    manifest.add_argument("--license-bundle", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "licenses":
        write_license_bundle(
            args.output, version=args.version, source_date_epoch=args.source_date_epoch,
            cargo=args.cargo, manifest=args.manifest_path,
        )
        print(args.output)
    else:
        result = write_manifest(
            args.artifact, args.output, version=args.version,
            signature_status=args.signature_status, rust_version=args.rust_version,
            source_date_epoch=args.source_date_epoch, source_commit=args.source_commit,
            source_dirty=args.source_dirty == "true",
            release_eligible=args.release_eligible == "true",
            license_bundle=args.license_bundle,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
