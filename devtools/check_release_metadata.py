"""Verify that every executable version resource uses the project version."""

from __future__ import annotations

import argparse
import ast
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
_EXACT_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s]+)")


def _exact_requirements(lines: list[str], *, source: str) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        match = _EXACT_REQUIREMENT.match(line)
        if match is None:
            raise ValueError(f"{source} contains an unpinned requirement: {line}")
        name, version = match.groups()
        requirements[name.casefold().replace("_", "-")] = version
    return requirements


def _python_constant(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            value = node.value
            if value is not None:
                return ast.literal_eval(value)
    raise ValueError(f"{name} was not found in {path}")


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

    project_requirements = _exact_requirements(
        [str(value) for value in project_data["project"].get("dependencies", [])],
        source="pyproject.toml",
    )
    runtime_requirements = _exact_requirements(
        (root / "requirements.txt").read_text(encoding="utf-8").splitlines(),
        source="requirements.txt",
    )
    if runtime_requirements != project_requirements:
        errors.append(
            "requirements.txt does not exactly match pinned project dependencies"
        )

    dev_requirements = _exact_requirements(
        (root / "requirements-dev.txt").read_text(encoding="utf-8").splitlines(),
        source="requirements-dev.txt",
    )
    for tool in ("pyinstaller", "pytest"):
        if tool not in dev_requirements:
            errors.append(f"requirements-dev.txt must pin {tool}")

    spec_text = (root / "quickaccess.spec").read_text(encoding="utf-8")
    for required in (
        'name="QuickAccess"',
        'version="version_info.txt"',
        'icon="assets/quickaccess.ico"',
        "upx=False",
        "console=False",
    ):
        if required not in spec_text:
            errors.append(f"quickaccess.spec is missing release invariant: {required}")
    if re.search(r"^\s*COLLECT\(", spec_text, re.MULTILINE):
        errors.append("quickaccess.spec must remain a one-file build (no COLLECT step)")

    build_text = (root / "build.ps1").read_text(encoding="utf-8")
    build_python_match = re.search(
        r'\$releaseVersion\s*=\s*\[version\]"([^"]+)"', build_text
    )
    build_pyinstaller_match = re.search(
        r'\$releasePyInstallerVersion\s*=\s*\[version\]"([^"]+)"', build_text
    )
    ci_text = (root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    ci_python_match = re.search(r'python-version:\s*"([^"]+)"', ci_text)
    build_python = build_python_match.group(1) if build_python_match else None
    ci_python = ci_python_match.group(1) if ci_python_match else None
    if build_python is None:
        errors.append("build.ps1 release Python version was not found")
    if ci_python is None:
        errors.append("CI Python version was not found")
    if build_python is not None and ci_python is not None and build_python != ci_python:
        errors.append(
            f"build Python is {build_python}, but CI Python is {ci_python}"
        )
    build_pyinstaller = (
        build_pyinstaller_match.group(1) if build_pyinstaller_match else None
    )
    required_pyinstaller = dev_requirements.get("pyinstaller")
    if build_pyinstaller is None:
        errors.append("build.ps1 release PyInstaller version was not found")
    elif build_pyinstaller != required_pyinstaller:
        errors.append(
            f"build PyInstaller is {build_pyinstaller}, but requirements-dev.txt "
            f"pins {required_pyinstaller}"
        )
    for invariant in (
        "$env:PYTHONHASHSEED",
        "$env:SOURCE_DATE_EPOCH",
        "devtools\\release_manifest.py",
    ):
        if invariant not in build_text:
            errors.append(f"build.ps1 is missing release invariant: {invariant}")

    sign_text = (root / "sign.ps1").read_text(encoding="utf-8")
    for invariant in (
        "QuickAccess.release.json",
        "TimeStamperCertificate",
        "Get-AuthenticodeSignature",
        "Get-FileHash",
        "SHA256",
    ):
        if invariant not in sign_text:
            errors.append(f"sign.ps1 is missing release invariant: {invariant}")

    runtime_repo = _python_constant(
        root / "quickaccess" / "services" / "update_check.py", "DEFAULT_REPO"
    )
    if runtime_repo != "Daksle-number-13/quickaccess-launcher":
        errors.append(f"unexpected update repository: {runtime_repo!r}")

    if errors:
        raise ValueError("Release metadata mismatch:\n- " + "\n- ".join(errors))
    return project_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("message", "plain"), default="message")
    args = parser.parse_args()
    version = check_release_metadata()
    if args.format == "plain":
        print(version)
    else:
        print(f"Release metadata is consistent: {version} ({version}.0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
