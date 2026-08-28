from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError, URLError

from quickaccess.services.update_check import (
    DEFAULT_REPO,
    UpdateCheckResult,
    UpdateCheckStatus,
    check_for_update,
)


def _fetch_returning(payload: dict[str, object]):
    def fetch(_url: str, _timeout: float) -> bytes:
        return json.dumps(payload).encode("utf-8")

    return fetch


class UpdateCheckTests(unittest.TestCase):
    def test_newer_release_is_reported_available(self) -> None:
        release_url = f"https://github.com/{DEFAULT_REPO}/releases/tag/v1.2.0"
        asset_url = (
            f"https://github.com/{DEFAULT_REPO}/releases/download/"
            "v1.2.0/QuickAccess.exe"
        )
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning(
                {
                    "tag_name": "v1.2.0",
                    "html_url": release_url,
                    "assets": [
                        {
                            "name": "QuickAccess.exe",
                            "browser_download_url": asset_url,
                            "size": 1234,
                            "digest": "sha256:" + "ab" * 32,
                        }
                    ],
                }
            ),
        )
        self.assertTrue(result.available)
        self.assertEqual(result.latest_version, "v1.2.0")
        self.assertEqual(result.status, UpdateCheckStatus.UPDATE_AVAILABLE)
        self.assertEqual(result.release_url, release_url)
        self.assertEqual(result.asset_url, asset_url)
        self.assertEqual(result.asset_size, 1234)
        self.assertEqual(result.asset_digest, "sha256:" + "ab" * 32)

    def test_equal_or_older_release_is_not_available(self) -> None:
        same = check_for_update("1.0.0", fetch=_fetch_returning({"tag_name": "1.0.0"}))
        older = check_for_update("2.0.0", fetch=_fetch_returning({"tag_name": "v1.9.9"}))
        self.assertFalse(same.available)
        self.assertFalse(older.available)
        self.assertEqual(same.status, UpdateCheckStatus.LATEST)
        self.assertEqual(older.status, UpdateCheckStatus.LATEST)

    def test_network_failure_is_reported_as_offline_and_never_raises(self) -> None:
        def failing_fetch(_url: str, _timeout: float) -> bytes:
            raise URLError("network unreachable")

        result = check_for_update("1.0.0", fetch=failing_fetch)
        self.assertFalse(result.available)
        self.assertEqual(result.status, UpdateCheckStatus.OFFLINE)
        self.assertEqual(result.error_reason, "offline")

    def test_malformed_response_is_reported_as_error(self) -> None:
        malformed = check_for_update(
            "1.0.0",
            fetch=lambda _url, _timeout: b"not json",
        )
        self.assertFalse(malformed.available)
        self.assertEqual(malformed.status, UpdateCheckStatus.ERROR)
        self.assertEqual(malformed.error_reason, "invalid_response")

    def test_http_failure_is_not_mislabeled_as_offline(self) -> None:
        def failing_fetch(url: str, _timeout: float) -> bytes:
            raise HTTPError(url, 403, "forbidden", hdrs=None, fp=None)

        result = check_for_update("1.0.0", fetch=failing_fetch)
        self.assertEqual(result.status, UpdateCheckStatus.ERROR)
        self.assertEqual(result.error_reason, "http_error")

    def test_missing_or_unparsable_tag_is_treated_as_no_update(self) -> None:
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning({"tag_name": "not-a-version"}),
        )
        self.assertFalse(result.available)
        self.assertEqual(result.status, UpdateCheckStatus.ERROR)
        self.assertEqual(result.error_reason, "invalid_version")

    def test_invalid_current_version_short_circuits_without_a_network_call(self) -> None:
        called = False

        def fetch(_url: str, _timeout: float) -> bytes:
            nonlocal called
            called = True
            return b"{}"

        result = check_for_update("not-a-version", fetch=fetch)
        self.assertFalse(result.available)
        self.assertEqual(result.status, UpdateCheckStatus.ERROR)
        self.assertFalse(called)

    def test_repository_and_returned_urls_are_origin_pinned(self) -> None:
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning(
                {
                    "tag_name": "v2.0.0",
                    "html_url": "https://evil.example/releases/tag/v2.0.0",
                    "assets": [
                        {
                            "name": "QuickAccess.exe",
                            "browser_download_url": (
                                "https://github.com/other/project/releases/download/"
                                "v2.0.0/QuickAccess.exe"
                            ),
                        }
                    ],
                }
            ),
        )
        self.assertTrue(result.available)
        self.assertIsNone(result.release_url)
        self.assertIsNone(result.asset_url)

        invalid_repo = check_for_update(
            "1.0.0", repo="owner/project?redirect=evil", fetch=_fetch_returning({})
        )
        self.assertEqual(invalid_repo.status, UpdateCheckStatus.ERROR)

    def test_wrong_asset_name_and_invalid_digest_are_not_trusted(self) -> None:
        tag = "v2.0.0"
        asset_url = (
            f"https://github.com/{DEFAULT_REPO}/releases/download/"
            f"{tag}/QuickAccess.exe"
        )
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning(
                {
                    "tag_name": tag,
                    "assets": [
                        {
                            "name": "QuickAccess.exe",
                            "browser_download_url": asset_url,
                            "digest": "sha256:not-a-real-hash",
                        }
                    ],
                }
            ),
        )
        self.assertEqual(result.asset_url, asset_url)
        self.assertIsNone(result.asset_digest)

    def test_response_size_is_bounded(self) -> None:
        result = check_for_update(
            "1.0.0", fetch=lambda _url, _timeout: b"x" * 1_048_577
        )
        self.assertEqual(result.status, UpdateCheckStatus.ERROR)
        self.assertEqual(result.error_reason, "invalid_response")

    def test_original_result_constructor_remains_compatible(self) -> None:
        available = UpdateCheckResult(True, "v2.0.0", "https://example.invalid")
        latest = UpdateCheckResult(False)
        self.assertEqual(available.status, UpdateCheckStatus.UPDATE_AVAILABLE)
        self.assertEqual(latest.status, UpdateCheckStatus.LATEST)


if __name__ == "__main__":
    unittest.main()
