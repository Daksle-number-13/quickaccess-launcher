from __future__ import annotations

import unittest
from unittest.mock import patch

from quickaccess.diagnostics import collect_diagnostics
from quickaccess.models import LauncherConfig, LauncherItem
from quickaccess.services.startup import (
    StartupRegistrationState,
    StartupRegistrationStatus,
)


class DiagnosticsTests(unittest.TestCase):
    def test_report_counts_types_and_omits_sensitive_targets(self) -> None:
        config = LauncherConfig(
            hotkey="ctrl+space",
            quick_add_hotkey="ctrl+shift+space",
            appearance_mode="dark",
            columns=4,
            run_on_startup=True,
            check_updates=False,
            items=[
                LauncherItem("개인 폴더", r"C:\Users\Secret\Private", "folder", 0),
                LauncherItem("급여 파일", r"C:\Users\Secret\salary.xlsx", "file", 1),
                LauncherItem("사내 URL", "https://private.example/token", "url", 2),
            ],
        )
        status = StartupRegistrationStatus(
            StartupRegistrationState.CORRECT,
            True,
            '"QuickAccess.exe" --startup',
            '"QuickAccess.exe" --startup',
        )

        with (
            patch("quickaccess.diagnostics.platform.system", return_value="Windows"),
            patch("quickaccess.diagnostics.platform.release", return_value="11"),
            patch("quickaccess.diagnostics.platform.version", return_value="test-build"),
            patch("quickaccess.diagnostics.platform.machine", return_value="AMD64"),
        ):
            snapshot = collect_diagnostics(config, startup_status=status)
            report = snapshot.render()

        self.assertEqual((1, 1, 1), (snapshot.folder_count, snapshot.file_count, snapshot.url_count))
        self.assertIn("바로가기: 3개 (폴더 1, 파일 1, 링크 1)", report)
        self.assertIn("실제 상태 correct", report)
        self.assertIn("화면: dark, 4열", report)
        self.assertNotIn("Secret", report)
        self.assertNotIn("salary", report)
        self.assertNotIn("private.example", report)

    def test_collection_rejects_non_config_and_handles_unknown_startup(self) -> None:
        with self.assertRaises(TypeError):
            collect_diagnostics(object())  # type: ignore[arg-type]

        snapshot = collect_diagnostics(LauncherConfig(items=[]))
        self.assertEqual("확인 안 됨", snapshot.startup_state)
        self.assertIn("개인 파일 경로", snapshot.render())


if __name__ == "__main__":
    unittest.main()
