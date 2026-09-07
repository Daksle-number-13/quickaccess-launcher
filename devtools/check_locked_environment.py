"""Verify that the active release environment matches pinned requirements."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def locked_requirements(
    path: Path,
    *,
    environment: dict[str, str] | None = None,
    _seen: set[Path] | None = None,
) -> dict[str, tuple[str, str]]:
    """Return active exact pins as canonical name -> (display name, version)."""

    resolved = path.resolve(strict=True)
    seen = set() if _seen is None else _seen
    if resolved in seen:
        return {}
    seen.add(resolved)
    marker_environment = default_environment() if environment is None else environment
    pins: dict[str, tuple[str, str]] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            include = line.split(maxsplit=1)[1]
            pins.update(
                locked_requirements(
                    resolved.parent / include,
                    environment=marker_environment,
                    _seen=seen,
                )
            )
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate(marker_environment):
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError(f"requirement is not an exact version pin: {line}")
        canonical = canonicalize_name(requirement.name)
        pin = (requirement.name, specifiers[0].version)
        previous = pins.get(canonical)
        if previous is not None and previous[1] != pin[1]:
            raise ValueError(
                f"conflicting pins for {requirement.name}: {previous[1]} and {pin[1]}"
            )
        pins[canonical] = pin
    return pins


def check_locked_environment(path: Path) -> dict[str, str]:
    pins = locked_requirements(path)
    errors: list[str] = []
    installed: dict[str, str] = {}
    for canonical, (display_name, expected) in sorted(pins.items()):
        try:
            actual = metadata.version(display_name)
        except metadata.PackageNotFoundError:
            errors.append(f"{display_name} is not installed (expected {expected})")
            continue
        installed[canonical] = actual
        if actual != expected:
            errors.append(f"{display_name} is {actual}, expected {expected}")
    if errors:
        raise RuntimeError("Release environment mismatch:\n- " + "\n- ".join(errors))
    return installed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "requirements",
        nargs="?",
        type=Path,
        default=ROOT / "requirements-dev.txt",
    )
    args = parser.parse_args()
    installed = check_locked_environment(args.requirements)
    print(f"Release environment matches {len(installed)} exact package pins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
