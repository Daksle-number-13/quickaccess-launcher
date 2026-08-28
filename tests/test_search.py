from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from quickaccess.models import LauncherItem
from quickaccess.search import LauncherSearchIndex, search_launcher_items


def _item(
    name: str,
    path: str,
    order: int,
    *,
    item_type: str = "file",
) -> LauncherItem:
    return LauncherItem(
        name=name,
        path=path,
        type=item_type,  # type: ignore[arg-type]
        order=order,
    )


class LauncherSearchTests(unittest.TestCase):
    def test_empty_query_returns_manual_order_with_stable_input_ties(self) -> None:
        items = [
            _item("third", r"C:\third", 2),
            _item("first-a", r"C:\first-a", 0),
            _item("first-b", r"C:\first-b", 0),
            _item("second", r"C:\second", 1),
        ]

        result = LauncherSearchIndex(items).search("  \t ")

        self.assertEqual(
            ["first-a", "first-b", "second", "third"],
            [item.name for item in result],
        )

    def test_unicode_compatibility_and_casefolding_are_applied(self) -> None:
        item = _item("Straße ＱＡ 보고서", r"C:\Reports\annual.xlsx", 0)

        result = search_launcher_items([item], "STRASSE qa")

        self.assertEqual([item], result)

    def test_korean_initial_consonants_match_name_and_path(self) -> None:
        name_match = _item(
            "김민수 검사 기준서",
            r"C:\Quality\standard.pdf",
            0,
        )
        path_match = _item(
            "주간 자료",
            r"C:\품질문서\생산일지.xlsx",
            1,
        )
        index = LauncherSearchIndex([name_match, path_match])

        self.assertEqual([name_match], index.search("ㄱㅁㅅ ㄱㅅㄱㅈㅅ"))
        self.assertEqual([path_match], index.search("ㅍㅈㅁㅅ ㅅㅅㅇㅈ"))

    def test_multi_token_matching_uses_and_across_name_path_and_hostname(self) -> None:
        mixed = _item(
            "Weekly dashboard",
            "https://quality.example.com/releases/2026",
            2,
            item_type="url",
        )
        missing_token = _item(
            "Weekly dashboard",
            "https://quality.example.com/archive",
            1,
            item_type="url",
        )
        index = LauncherSearchIndex([missing_token, mixed])

        self.assertEqual([mixed], index.search("weekly quality 2026"))
        self.assertEqual([], index.search("weekly quality missing"))

    def test_ranking_prefers_exact_prefix_and_name_substring_before_path(self) -> None:
        path_only = _item("Documents", r"C:\Report\current.pdf", 0)
        substring = _item("Weekly report", r"C:\weekly.pdf", 1)
        prefix_later = _item("Report archive", r"C:\archive", 4)
        prefix_earlier = _item("Report templates", r"C:\templates", 3)
        exact = _item("Report", r"C:\report.pdf", 9)
        index = LauncherSearchIndex(
            [path_only, substring, prefix_later, exact, prefix_earlier]
        )

        result = index.search("REPORT")

        self.assertEqual(
            [exact, prefix_earlier, prefix_later, substring, path_only],
            result,
        )

    def test_korean_initial_ranking_treats_exact_and_prefix_names_first(self) -> None:
        path_only = _item("기타", r"C:\김민수\문서", 0)
        substring = _item("팀 김민수", r"C:\team", 1)
        prefix = _item("김민수 문서", r"C:\docs", 2)
        exact = _item("김민수", r"C:\profile", 3)

        result = LauncherSearchIndex(
            [path_only, substring, prefix, exact]
        ).search("ㄱㅁㅅ")

        self.assertEqual([exact, prefix, substring, path_only], result)

    def test_reused_100_item_index_is_fast_and_does_no_io(self) -> None:
        items = [
            _item(
                f"품질 보고서 {index:03d}",
                rf"C:\Quality\2026\report-{index:03d}.xlsx",
                index,
            )
            for index in range(100)
        ]

        with (
            patch.object(
                os.path,
                "exists",
                side_effect=AssertionError("filesystem access"),
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network access"),
            ),
        ):
            search_index = LauncherSearchIndex(items)
            started = time.perf_counter()
            for _ in range(500):
                result = search_index.search("ㅍㅈ 보고 099")
            elapsed = time.perf_counter() - started

        self.assertEqual([items[99]], result)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
