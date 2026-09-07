"""Build the third-party license companion shipped with binary releases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devtools.check_locked_environment import locked_requirements  # noqa: E402


_LICENSE_BASENAME = re.compile(r"^(?:license|copying|notice)(?:[._-].*)?$", re.I)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return normalized.strip("-.") or "component"


def _license_files_for_distribution(name: str) -> list[tuple[str, bytes]]:
    distribution = importlib.metadata.distribution(name)
    component = _safe_component(distribution.metadata.get("Name") or name)
    version = _safe_component(distribution.version)
    entries: list[tuple[str, bytes]] = []
    for relative in distribution.files or ():
        relative_text = str(relative).replace("\\", "/")
        basename = PurePosixPath(relative_text).name
        if not _LICENSE_BASENAME.match(basename):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        parent_hint = _safe_component(PurePosixPath(relative_text).parent.name)
        archive_name = f"{component}-{version}-{parent_hint}-{_safe_component(basename)}"
        entries.append((archive_name, source.read_bytes()))
    if not entries:
        raise FileNotFoundError(f"no license files found for {name} {distribution.version}")
    return entries


def collect_license_entries(root: Path = ROOT) -> dict[str, bytes]:
    """Return release license files keyed by deterministic archive names."""

    entries: dict[str, bytes] = {
        "QuickAccess-LICENSE.txt": (root / "LICENSE").read_bytes(),
        "THIRD_PARTY_NOTICES.md": (root / "THIRD_PARTY_NOTICES.md").read_bytes(),
    }
    release_pins = locked_requirements(root / "requirements-dev.txt")
    entries["PACKAGE_VERSIONS.txt"] = (
        "\n".join(
            f"{display_name}=={version}"
            for display_name, version in sorted(
                release_pins.values(),
                key=lambda item: item[0].casefold(),
            )
        )
        + "\n"
    ).encode("utf-8")
    for distribution_name, _version in sorted(
        release_pins.values(),
        key=lambda item: item[0].casefold(),
    ):
        for archive_name, payload in _license_files_for_distribution(distribution_name):
            unique_name = archive_name
            suffix = 2
            while unique_name in entries:
                stem, dot, extension = archive_name.rpartition(".")
                unique_name = (
                    f"{stem}-{suffix}.{extension}"
                    if dot
                    else f"{archive_name}-{suffix}"
                )
                suffix += 1
            entries[unique_name] = payload

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"Python license file was not found: {python_license}")
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    entries[f"Python-{python_version}-LICENSE.txt"] = python_license.read_bytes()

    tcl_candidates = sorted((Path(sys.base_prefix) / "tcl").glob("tk*/license.terms"))
    if not tcl_candidates:
        raise FileNotFoundError("Tcl/Tk license.terms was not found in the release runtime")
    entries["Tcl-Tk-LICENSE.terms"] = tcl_candidates[0].read_bytes()

    apache_source = next(
        (payload for name, payload in entries.items() if "LICENSE.APACHE" in name.upper()),
        None,
    )
    if apache_source is None:
        raise FileNotFoundError("Apache License 2.0 text required by bundled Roboto was not found")
    entries["Roboto-Apache-2.0-LICENSE.txt"] = apache_source
    return entries


def write_license_bundle(
    output: Path,
    *,
    version: str,
    source_date_epoch: int,
    root: Path = ROOT,
) -> Path:
    if source_date_epoch < 0:
        raise ValueError("source date epoch must not be negative")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must use numeric MAJOR.MINOR.PATCH form")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    timestamp = datetime.fromtimestamp(
        max(source_date_epoch, 315532800), timezone.utc
    ).timetuple()[:6]
    entries = collect_license_entries(root)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries, key=str.casefold):
            info = zipfile.ZipInfo(f"QuickAccess-{version}-licenses/{name}", timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    temporary.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    args = parser.parse_args()
    output = write_license_bundle(
        args.output,
        version=args.version,
        source_date_epoch=args.source_date_epoch,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
