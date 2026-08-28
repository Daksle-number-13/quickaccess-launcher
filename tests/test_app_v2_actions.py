from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from quickaccess.app import QuickAccessApp
from quickaccess.models import LauncherConfig
from quickaccess.services.explorer import ExplorerTarget, ExplorerTargetResult
from quickaccess.services.startup import (
    StartupRegistrationState,
    StartupRegistrationStatus,
)


class _ToastRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def show(self, message: str, **options: object) -> None:
        self.messages.append((message, options))


class _StoreRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.saved: list[LauncherConfig] = []

    def save(self, config: LauncherConfig) -> None:
        if self.fail:
            raise OSError("disk full")
        self.saved.append(deepcopy(config))


class _ValidatorRecorder:
    def __init__(self) -> None:
        self.validated: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    def validate(self, item_id: str, path: str) -> None:
        self.validated.append((item_id, path))

    def cancel(self, item_id: str) -> None:
        self.cancelled.append(item_id)


class _IconRecorder:
    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    def request(self, key: str, path: str) -> None:
        self.requested.append((key, path))


class _QuickAddHarness(QuickAccessApp):
    def __init__(self) -> None:
        self.config = LauncherConfig(items=[])
        self.store = _StoreRecorder()
        self.toast = _ToastRecorder()
        self.validator = _ValidatorRecorder()
        self.icons = _IconRecorder()
        self.statuses: dict[str, object] = {}
        self.settings = None
        self._quick_add_inflight = True
        self._quick_add_generation = 3
        self._quick_add_timeout_after = None
        self.refresh_calls = 0

    def _refresh_visible_popup(self, *, layout_required: bool = False) -> None:
        if layout_required:
            self.refresh_calls += 1

    @staticmethod
    def _dialog_parent() -> object:
        return object()


class _ImportHarness(QuickAccessApp):
    def __init__(self, *, save_fails: bool = False) -> None:
        self.config = LauncherConfig(items=[])
        self.config.hotkey = "ctrl+space"
        self.config.quick_add_hotkey = "ctrl+shift+space"
        self.config.appearance_mode = "system"
        self.config.run_on_startup = False
        self.store = _StoreRecorder(fail=save_fails)
        self.toast = _ToastRecorder()
        self.statuses: dict[str, object] = {}
        self.validator = _ValidatorRecorder()
        self.icons = _IconRecorder()
        self._startup_status = None
        self._update_check_generation = 0
        self.hotkeys = SimpleNamespace(bindings={})
        self.hotkey_calls: list[tuple[str, str]] = []
        self.startup_calls: list[bool] = []
        self.refresh_calls = 0
        self.validation_sweeps = 0
        self.icon_sweeps = 0
        self.update_checks = 0
        self.update_cancels = 0
        self.startup = self

    def _configure_hotkeys(
        self, panel: str, quick_add: str, *, show_error: bool
    ) -> bool:
        self.hotkey_calls.append((panel, quick_add))
        self.hotkeys.bindings = {"panel": panel, "quick_add": quick_add}
        return True

    def reconcile(
        self, desired: bool, _executable: str, _arguments: tuple[str, ...]
    ) -> StartupRegistrationStatus:
        self.startup_calls.append(desired)
        return StartupRegistrationStatus(
            state=(
                StartupRegistrationState.CORRECT
                if desired
                else StartupRegistrationState.ABSENT
            ),
            desired_enabled=desired,
            expected_command="QuickAccess.exe --startup",
        )

    def _validate_all_paths(self) -> None:
        self.validation_sweeps += 1

    def _request_all_icons(self) -> None:
        self.icon_sweeps += 1

    def _refresh_visible_popup(self, *, layout_required: bool = False) -> None:
        if layout_required:
            self.refresh_calls += 1

    def _check_for_update(self) -> None:
        self.update_checks += 1

    def _cancel_update_check_schedule(self) -> None:
        self.update_cancels += 1


class _ClipboardRoot:
    def __init__(self) -> None:
        self.value = ""
        self.updated = 0

    def clipboard_clear(self) -> None:
        self.value = ""

    def clipboard_append(self, value: str) -> None:
        self.value += value

    def update_idletasks(self) -> None:
        self.updated += 1


class AppV2ActionTests(unittest.TestCase):
    def test_multi_selection_uses_one_save_and_one_layout_refresh(self) -> None:
        harness = _QuickAddHarness()
        result = ExplorerTargetResult(
            success=True,
            path=r"C:\One.txt",
            suggested_name="One.txt",
            item_type="file",
            targets=(
                ExplorerTarget(r"C:\One.txt", "One.txt", "file"),
                ExplorerTarget(r"C:\Folder", "Folder", "folder"),
            ),
        )

        with patch("quickaccess.app.ask_display_name") as ask_name:
            harness._finish_quick_add((3, result))

        ask_name.assert_not_called()
        self.assertEqual(1, len(harness.store.saved))
        self.assertEqual(1, harness.refresh_calls)
        self.assertEqual(2, len(harness.config.items))
        self.assertEqual(2, len(harness.validator.validated))
        self.assertEqual(2, len(harness.icons.requested))
        self.assertIn("2개", harness.toast.messages[-1][0])

    def test_multi_selection_skips_existing_targets_without_partial_save(self) -> None:
        harness = _QuickAddHarness()
        harness.config.add_item(r"C:\One.txt", name="기존", item_type="file")
        result = ExplorerTargetResult(
            success=True,
            path=r"c:\ONE.txt",
            suggested_name="ONE.txt",
            item_type="file",
            targets=(ExplorerTarget(r"c:\ONE.txt", "ONE.txt", "file"),),
        )

        with patch("quickaccess.app.ask_display_name", return_value="새 이름"):
            harness._finish_quick_add((3, result))

        # Single selection intentionally keeps the legacy name-confirmation flow.
        self.assertEqual(1, len(harness.store.saved))

        harness = _QuickAddHarness()
        harness.config.add_item(r"C:\One.txt", name="기존", item_type="file")
        added = harness._add_quick_add_targets(
            (
                ExplorerTarget(r"c:\ONE.txt", "ONE.txt", "file"),
                ExplorerTarget(r"C:\one.TXT", "one.TXT", "file"),
            )
        )
        self.assertFalse(added)
        self.assertEqual([], harness.store.saved)
        self.assertIn("이미 등록", harness.toast.messages[-1][0])

    def test_multi_selection_save_failure_adds_nothing(self) -> None:
        harness = _QuickAddHarness()
        harness.store.fail = True

        added = harness._add_quick_add_targets(
            (
                ExplorerTarget(r"C:\One.txt", "One.txt", "file"),
                ExplorerTarget(r"C:\Two.txt", "Two.txt", "file"),
            )
        )

        self.assertFalse(added)
        self.assertEqual([], harness.config.items)
        self.assertEqual([], harness.validator.validated)
        self.assertEqual([], harness.icons.requested)
        self.assertEqual(0, harness.refresh_calls)

    def test_import_runtime_failure_rolls_back_without_replacing_config(self) -> None:
        harness = _ImportHarness(save_fails=True)
        candidate = deepcopy(harness.config)
        candidate.hotkey = "ctrl+alt+space"
        candidate.quick_add_hotkey = "ctrl+alt+q"
        candidate.appearance_mode = "dark"
        candidate.run_on_startup = True

        with (
            patch(
                "quickaccess.app.startup_invocation",
                return_value=(r"C:\QuickAccess.exe", ("--startup",)),
            ),
            patch("quickaccess.app.apply_appearance_mode") as appearance,
        ):
            applied = harness._apply_imported_config(candidate)

        self.assertFalse(applied)
        self.assertEqual("ctrl+space", harness.config.hotkey)
        self.assertEqual([True, False], harness.startup_calls)
        self.assertEqual(
            [
                ("ctrl+alt+space", "ctrl+alt+q"),
                ("ctrl+space", "ctrl+shift+space"),
            ],
            harness.hotkey_calls,
        )
        self.assertEqual(["dark", "system"], [call.args[0] for call in appearance.call_args_list])
        self.assertIn("가져오지 못했습니다", harness.toast.messages[-1][0])

    def test_failed_import_hotkey_attempt_always_restores_previous_pair(self) -> None:
        harness = _ImportHarness()
        candidate = deepcopy(harness.config)
        candidate.hotkey = "ctrl+alt+space"
        candidate.quick_add_hotkey = "ctrl+alt+q"
        configure_results = iter((False, True))

        def configure(panel: str, quick_add: str, *, show_error: bool) -> bool:
            harness.hotkey_calls.append((panel, quick_add))
            return next(configure_results)

        harness._configure_hotkeys = configure  # type: ignore[method-assign]

        applied = harness._apply_imported_config(candidate)

        self.assertFalse(applied)
        self.assertEqual(
            [
                ("ctrl+alt+space", "ctrl+alt+q"),
                ("ctrl+space", "ctrl+shift+space"),
            ],
            harness.hotkey_calls,
        )
        self.assertEqual("ctrl+space", harness.config.hotkey)

    def test_successful_import_reconfigures_runtime_once(self) -> None:
        harness = _ImportHarness()
        candidate = deepcopy(harness.config)
        candidate.check_updates = True
        candidate.add_item(r"C:\New.txt", name="New", item_type="file")

        with patch(
            "quickaccess.app.startup_invocation",
            return_value=(r"C:\QuickAccess.exe", ("--startup",)),
        ):
            applied = harness._apply_imported_config(candidate)

        self.assertTrue(applied)
        self.assertEqual(1, len(harness.store.saved))
        self.assertEqual(1, harness.validation_sweeps)
        self.assertEqual(1, harness.icon_sweeps)
        self.assertEqual(1, harness.refresh_calls)
        self.assertEqual(1, harness.update_checks)

    def test_copy_diagnostics_excludes_registered_target_paths(self) -> None:
        harness = _ImportHarness()
        harness.root = _ClipboardRoot()
        harness.config.add_item(
            r"C:\Users\Private\secret.txt",
            name="비밀 파일",
            item_type="file",
        )
        harness.get_startup_status = MagicMock(
            return_value=StartupRegistrationStatus(
                state=StartupRegistrationState.ABSENT,
                desired_enabled=False,
                expected_command="QuickAccess.exe --startup",
            )
        )

        copied = harness.copy_diagnostics()

        self.assertTrue(copied)
        self.assertIn("QuickAccess 진단 정보", harness.root.value)
        self.assertNotIn("Private", harness.root.value)
        self.assertNotIn("secret.txt", harness.root.value)
        self.assertEqual(1, harness.root.updated)

    def test_export_callback_uses_atomic_portable_writer(self) -> None:
        harness = _ImportHarness()
        harness.settings = None
        harness.root = SimpleNamespace()

        with (
            patch(
                "quickaccess.app.filedialog.asksaveasfilename",
                return_value=r"C:\Backup\quickaccess-settings.json",
            ),
            patch("quickaccess.app.write_portable_config") as write_config,
        ):
            exported = harness.export_config()

        self.assertTrue(exported)
        write_config.assert_called_once_with(
            r"C:\Backup\quickaccess-settings.json",
            harness.config,
        )
        self.assertIn("저장했습니다", harness.toast.messages[-1][0])

    def test_settings_actions_expose_all_v2_callbacks(self) -> None:
        harness = _ImportHarness()

        actions = harness._build_settings_actions()

        self.assertIsNotNone(actions.import_config)
        self.assertIsNotNone(actions.export_config)
        self.assertIsNotNone(actions.copy_diagnostics)


if __name__ == "__main__":
    unittest.main()
