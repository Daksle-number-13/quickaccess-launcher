from __future__ import annotations

import json
import unittest

from quickaccess.services.update_check import check_for_update


def _fetch_returning(payload: dict[str, object]):
    def fetch(_url: str, _timeout: float) -> bytes:
        return json.dumps(payload).encode("utf-8")

    return fetch


class UpdateCheckTests(unittest.TestCase):
    def test_newer_release_is_reported_available(self) -> None:
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning(
                {"tag_name": "v1.2.0", "html_url": "https://example.invalid/releases/v1.2.0"}
            ),
        )
        self.assertTrue(result.available)
        self.assertEqual(result.latest_version, "v1.2.0")
        self.assertEqual(result.release_url, "https://example.invalid/releases/v1.2.0")

    def test_equal_or_older_release_is_not_available(self) -> None:
        same = check_for_update("1.0.0", fetch=_fetch_returning({"tag_name": "1.0.0"}))
        older = check_for_update("2.0.0", fetch=_fetch_returning({"tag_name": "v1.9.9"}))
        self.assertFalse(same.available)
        self.assertFalse(older.available)

    def test_network_or_parse_failure_never_raises(self) -> None:
        def failing_fetch(_url: str, _timeout: float) -> bytes:
            raise OSError("network unreachable")

        result = check_for_update("1.0.0", fetch=failing_fetch)
        self.assertFalse(result.available)

        malformed = check_for_update(
            "1.0.0",
            fetch=lambda _url, _timeout: b"not json",
        )
        self.assertFalse(malformed.available)

    def test_missing_or_unparsable_tag_is_treated_as_no_update(self) -> None:
        result = check_for_update(
            "1.0.0",
            fetch=_fetch_returning({"tag_name": "not-a-version"}),
        )
        self.assertFalse(result.available)

    def test_invalid_current_version_short_circuits_without_a_network_call(self) -> None:
        called = False

        def fetch(_url: str, _timeout: float) -> bytes:
            nonlocal called
            called = True
            return b"{}"

        result = check_for_update("not-a-version", fetch=fetch)
        self.assertFalse(result.available)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
