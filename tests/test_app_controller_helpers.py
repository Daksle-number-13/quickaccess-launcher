from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from quickaccess.app import QuickAccessApp
from quickaccess.commands import CommandBus, LaunchResultCommand
from quickaccess.models import LauncherConfig
from quickaccess.services.launcher import FileLauncher
from quickaccess.services.update_check import UpdateCheckResult


class _PublishHarness:
    def __init__(self, config: LauncherConfig, launcher: FileLauncher) -> None:
        self.config = config
        self.launcher = launcher
        self.bus = CommandBus()

    def _safe_publish(self, command: object) -> None:
        self.bus.publish(command)  # type: ignore[arg-type]


class _ToastRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def show(self, message: str, **options: object) -> None:
        self.messages.append((message, options))


class _HotkeyRollbackHarness:
    def __init__(self) -> None:
        self.config = LauncherConfig.default()
        self.hotkeys = type(
            "Hotkeys",
            (),
            {"bindings": {"panel": "ctrl+alt+space", "quick_add": "ctrl+shift+space"}},
        )()
        self.toast = _ToastRecorder()
        self.calls: list[tuple[str, str, bool]] = []

    def _configure_hotkeys(
        self, panel: str, quick_add: str, *, show_error: bool
    ) -> bool:
        self.calls.append((panel, quick_add, show_error))
        return len(self.calls) == 1

    @staticmethod
    def _commit(_mutator: object, _message: str) -> bool:
        return False


class _StartupRollbackHarness:
    def __init__(self) -> None:
        self.config = LauncherConfig.default()
        self.toast = _ToastRecorder()
        self.calls = 0
        self.startup = self

    def set_enabled(self, _enabled: bool, _executable: str, _arguments: object) -> None:
        self.calls += 1
        if self.calls == 2:
            raise OSError("registry rollback denied")

    @staticmethod
    def _commit(_mutator: object, _message: str) -> bool:
        return False


class _AfterIdleRoot:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def after_idle(self, callback: object) -> str:
        self.callbacks.append(callback)
        return f"after-{len(self.callbacks)}"


class _PopupRefreshHarness:
    def __init__(self) -> None:
        self.root = _AfterIdleRoot()
        self.popup = type("Popup", (), {"visible": True})()
        self._popup_refresh_after = None

    @staticmethod
    def _flush_popup_refresh() -> None:
        pass


class _AppearanceHarness:
    def __init__(self, *, commit_succeeds: bool) -> None:
        self.config = LauncherConfig.default()
        self.commit_succeeds = commit_succeeds

    def _commit(self, mutator: object, _message: str) -> bool:
        if not self.commit_succeeds:
            return False
        mutator(self.config)  # type: ignore[operator]
        return True


class _ValidatorRecorder:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.validated: list[tuple[str, str]] = []

    def cancel(self, item_id: str) -> None:
        self.cancelled.append(item_id)

    def validate(self, item_id: str, path: str) -> None:
        self.validated.append((item_id, path))


class _IconRequestRecorder:
    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    def request(self, key: str, path: str) -> None:
        self.requested.append((key, path))


class _DeleteUndoHarness:
    def __init__(self) -> None:
        self.config = LauncherConfig(items=[])
        self.config.add_item(r"C:\Keep", name="유지")
        self.target = self.config.add_item(r"C:\Delete", name="삭제 대상")
        self.statuses: dict[str, object] = {self.target.id: "stale"}
        self.toast = _ToastRecorder()
        self.settings = None
        self.validator = _ValidatorRecorder()
        self.icons = _IconRequestRecorder()

    def _commit(self, mutator: object, _message: str) -> bool:
        mutator(self.config)  # type: ignore[operator]
        return True

    def _restore_deleted_item(
        self, name: str, path: str, item_type: str, order: int
    ) -> None:
        QuickAccessApp._restore_deleted_item(self, name, path, item_type, order)  # type: ignore[arg-type]


class _UpdateCheckHarness:
    def __init__(self) -> None:
        self.config = LauncherConfig.default()
        self.toast = _ToastRecorder()

    def _commit(self, mutator: object, _message: str) -> bool:
        mutator(self.config)  # type: ignore[operator]
        return True


class ControllerResponsivenessTests(unittest.TestCase):
    def test_visible_popup_refreshes_are_coalesced_until_idle(self) -> None:
        harness = _PopupRefreshHarness()

        QuickAccessApp._refresh_visible_popup(harness)  # type: ignore[arg-type]
        QuickAccessApp._refresh_visible_popup(harness)  # type: ignore[arg-type]

        self.assertEqual(len(harness.root.callbacks), 1)
        self.assertEqual(harness._popup_refresh_after, "after-1")

    def test_activate_item_does_not_block_caller_when_shell_launch_stalls(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_startfile(_path: str) -> None:
            entered.set()
            release.wait(2.0)

        config = LauncherConfig(items=[])
        item = config.add_item(r"\\server\offline\문서.xlsx", name="네트워크 문서")
        harness = _PublishHarness(config, FileLauncher(blocking_startfile))

        started = time.monotonic()
        QuickAccessApp.activate_item(harness, item.id)  # type: ignore[arg-type]
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(harness.bus.empty())
        release.set()

        deadline = time.monotonic() + 1.0
        command = None
        while command is None and time.monotonic() < deadline:
            drained = harness.bus.drain()
            command = drained[0] if drained else None
            if command is None:
                time.sleep(0.01)
        self.assertIsInstance(command, LaunchResultCommand)

    def test_hotkey_rollback_failure_is_visible_to_user(self) -> None:
        harness = _HotkeyRollbackHarness()

        result = QuickAccessApp.set_hotkeys(
            harness, "ctrl+alt+space", "ctrl+shift+space"  # type: ignore[arg-type]
        )

        self.assertFalse(result)
        self.assertEqual(len(harness.calls), 2)
        self.assertIn("복구하지 못했습니다", harness.toast.messages[-1][0])

    def test_startup_rollback_failure_is_visible_to_user(self) -> None:
        harness = _StartupRollbackHarness()

        result = QuickAccessApp.set_startup(harness, True)  # type: ignore[arg-type]

        self.assertFalse(result)
        self.assertEqual(harness.calls, 2)
        self.assertIn("복구하지 못했습니다", harness.toast.messages[-1][0])

    def test_appearance_is_applied_only_after_persistence_succeeds(self) -> None:
        saved = _AppearanceHarness(commit_succeeds=True)
        failed = _AppearanceHarness(commit_succeeds=False)

        with patch("quickaccess.app.ctk.set_appearance_mode") as apply_mode:
            self.assertTrue(
                QuickAccessApp.set_appearance_mode(saved, "light")  # type: ignore[arg-type]
            )
            self.assertEqual("light", saved.config.appearance_mode)
            apply_mode.assert_called_once_with("Light")

            apply_mode.reset_mock()
            self.assertFalse(
                QuickAccessApp.set_appearance_mode(failed, "dark")  # type: ignore[arg-type]
            )
            self.assertEqual("system", failed.config.appearance_mode)
            apply_mode.assert_not_called()

    def test_delete_offers_undo_that_restores_original_order(self) -> None:
        harness = _DeleteUndoHarness()
        target_id = harness.target.id

        result = QuickAccessApp.delete_item(harness, target_id)  # type: ignore[arg-type]

        self.assertTrue(result)
        self.assertEqual(1, len(harness.config.items))
        self.assertNotIn(target_id, harness.statuses)
        self.assertIn(target_id, harness.validator.cancelled)
        message, options = harness.toast.messages[-1]
        self.assertIn("삭제했습니다", message)
        self.assertEqual("실행취소", options["action_text"])

        options["action_command"]()  # type: ignore[operator]

        self.assertEqual(2, len(harness.config.items))
        restored = harness.config.items[1]
        self.assertEqual("삭제 대상", restored.name)
        self.assertEqual(r"C:\Delete", restored.path)
        self.assertEqual(1, restored.order)
        self.assertIn((restored.id, restored.path), harness.validator.validated)
        self.assertIn("복구했습니다", harness.toast.messages[-1][0])

    def test_update_available_shows_a_toast_with_a_download_action(self) -> None:
        harness = _UpdateCheckHarness()
        result = UpdateCheckResult(
            available=True,
            latest_version="v9.9.9",
            release_url="https://example.invalid/releases/v9.9.9",
        )

        QuickAccessApp._apply_update_check(harness, result)  # type: ignore[arg-type]

        self.assertEqual("v9.9.9", harness.config.last_update_notice)
        message, options = harness.toast.messages[-1]
        self.assertIn("v9.9.9", message)
        self.assertEqual("다운로드 페이지", options["action_text"])

    def test_update_notice_for_the_same_version_is_shown_only_once(self) -> None:
        harness = _UpdateCheckHarness()
        harness.config.last_update_notice = "v9.9.9"
        result = UpdateCheckResult(available=True, latest_version="v9.9.9")

        QuickAccessApp._apply_update_check(harness, result)  # type: ignore[arg-type]

        self.assertEqual([], harness.toast.messages)

    def test_unavailable_update_result_shows_no_toast(self) -> None:
        harness = _UpdateCheckHarness()

        QuickAccessApp._apply_update_check(  # type: ignore[arg-type]
            harness, UpdateCheckResult(available=False)
        )

        self.assertEqual([], harness.toast.messages)


if __name__ == "__main__":
    unittest.main()
