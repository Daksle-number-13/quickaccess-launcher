from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from quickaccess.app import QuickAccessApp
from quickaccess.commands import CommandBus, LaunchResultCommand
from quickaccess.models import LauncherConfig
from quickaccess.services.launcher import FileLauncher


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


if __name__ == "__main__":
    unittest.main()
