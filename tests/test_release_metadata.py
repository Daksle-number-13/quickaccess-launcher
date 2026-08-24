from pathlib import Path

from devtools.check_release_metadata import check_release_metadata


ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_matches_project_version() -> None:
    check_release_metadata(ROOT)
