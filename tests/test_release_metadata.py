from pathlib import Path
import shutil
import uuid

import pytest

from devtools.check_release_metadata import check_release_metadata


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_tmp() -> Path:
    path = ROOT / ".test-work" / f"release-metadata-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_release_metadata_matches_project_version() -> None:
    check_release_metadata(ROOT)


def _copy_release_metadata_tree(destination: Path) -> None:
    for relative in (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "quickaccess.spec",
        "build.ps1",
        "sign.ps1",
        "version_info.txt",
        ".github/workflows/ci.yml",
        "quickaccess/__init__.py",
        "quickaccess/services/update_check.py",
    ):
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def test_release_metadata_rejects_dependency_drift(release_tmp: Path) -> None:
    _copy_release_metadata_tree(release_tmp)
    requirements = release_tmp / "requirements.txt"
    requirements.write_text(
        requirements.read_text(encoding="utf-8").replace("Pillow==12.3.0", "Pillow==1.0.0"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requirements.txt"):
        check_release_metadata(release_tmp)


def test_release_metadata_rejects_onedir_spec(release_tmp: Path) -> None:
    _copy_release_metadata_tree(release_tmp)
    spec = release_tmp / "quickaccess.spec"
    spec.write_text(spec.read_text(encoding="utf-8") + "\nCOLLECT(exe)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="one-file"):
        check_release_metadata(release_tmp)


def test_release_metadata_rejects_build_ci_python_drift(release_tmp: Path) -> None:
    _copy_release_metadata_tree(release_tmp)
    workflow = release_tmp / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            'python-version: "3.13.15"', 'python-version: "3.12.0"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="build Python"):
        check_release_metadata(release_tmp)
