"""Verify that every executable version resource uses the project version."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_VERSION = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
_FIXED_VERSION = re.compile(
    r"^\s*(filevers|prodvers)=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\),",
    re.MULTILINE,
)
_STRING_VERSION = re.compile(
    r"StringStruct\(u'(FileVersion|ProductVersion)',\s*u'([^']+)'\)"
)


def check_release_metadata(root: Path = ROOT) -> str:
    """Return the project version or raise when release metadata has drifted."""

    project_data = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project_version = str(project_data["project"]["version"])
    version_parts = project_version.split(".")
    if len(version_parts) != 3 or not all(part.isdigit() for part in version_parts):
        raise ValueError(f"project version must be numeric MAJOR.MINOR.PATCH: {project_version}")

    package_text = (root / "quickaccess" / "__init__.py").read_text(encoding="utf-8")
    package_match = _PACKAGE_VERSION.search(package_text)
    if package_match is None:
        raise ValueError("quickaccess.__version__ was not found")
    package_version = package_match.group(1)

    version_text = (root / "version_info.txt").read_text(encoding="utf-8")
    fixed_versions = {
        name: tuple(int(part) for part in values)
        for name, *values in _FIXED_VERSION.findall(version_text)
    }
    string_versions = dict(_STRING_VERSION.findall(version_text))

    expected_fixed = tuple(int(part) for part in version_parts) + (0,)
    expected_string = f"{project_version}.0"
    errors: list[str] = []
    if package_version != project_version:
        errors.append(
            f"quickaccess.__version__ is {package_version}, expected {project_version}"
        )
    for field in ("filevers", "prodvers"):
        actual = fixed_versions.get(field)
        if actual != expected_fixed:
            errors.append(f"{field} is {actual}, expected {expected_fixed}")
    for field in ("FileVersion", "ProductVersion"):
        actual = string_versions.get(field)
        if actual != expected_string:
            errors.append(f"{field} is {actual!r}, expected {expected_string!r}")

    if errors:
        raise ValueError("Release metadata mismatch:\n- " + "\n- ".join(errors))
    return project_version


def main() -> int:
    version = check_release_metadata()
    print(f"Release metadata is consistent: {version} ({version}.0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
