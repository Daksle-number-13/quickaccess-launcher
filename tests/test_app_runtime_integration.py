from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from quickaccess.app import MUTEX_NAME, QuickAccessApp, _parse_args, main
from quickaccess.commands import CommandBus
from quickaccess.models import LauncherConfig
from quickaccess.services.singleton import InstanceRequest
from quickaccess.services.startup import (
    StartupRegistrationState,
    StartupRegistrationStatus,
)
from quickaccess.ui.settings import SettingsActions


def _startup_status(
    state: StartupRegistrationState,
    desired_enabled: bool,
    *,
    error: str | None = None,
) -> StartupRegistrationStatus:
    return StartupRegistrationStatus(
        state=state,
        desired_enabled=desired_enabled,
        expected_command=r'"C:\New\QuickAccess.exe" --startup',
        actual_command=(
            r'"C:\Old\QuickAccess.exe" --startup'
            if state is StartupRegistrationState.STALE
            else None
        ),
        error=error,
    )


class _ToastRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def show(self, message: str, **options: object) -> None:
        self.messages.append((message, options))


class _ScriptedStartup:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.reconcile_calls: list[tuple[bool, str, tuple[str, ...]]] = []
        self.inspect_calls: list[tuple[bool, str, tuple[str, ...]]] = []

    def reconcile(
        self, desired: bool, executable: str, arguments: tuple[str, ...]
    ) -> StartupRegistrationStatus:
        self.reconcile_calls.append((desired, executable, arguments))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, StartupRegistrationStatus)
        return response

    def inspect(
        self, desired: bool, executable: str, arguments: tuple[str, ...]
    ) -> StartupRegistrationStatus:
        self.inspect_calls.append((desired, executable, arguments))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, StartupRegistrationStatus)
        return response


class _StartupControllerHarness(QuickAccessApp):
    def __init__(
        self,
        startup: _ScriptedStartup,
        *,
        desired: bool,
        commit_succeeds: bool = True,
    ) -> None:
        self.config = LauncherConfig.default()
        self.config.run_on_startup = desired
        self.startup = startup
        self.toast = _ToastRecorder()
        self._startup_status: StartupRegistrationStatus | None = None
        self.commit_succeeds = commit_succeeds
        self.root = SimpleNamespace(scheduled=[])

        def after(delay: int, callback: object) -> str:
            self.root.scheduled.append((delay, callback))
            return f"after-{len(self.root.scheduled)}"

        self.root.after = after

    def _commit(self, mutator: object, _message: str) -> bool:
        if not self.commit_succeeds:
            return False
        mutator(self.config)  # type: ignore[operator]
        return True


class _RequestGuard:
    def __init__(self, requests: tuple[InstanceRequest, ...]) -> None:
        self.requests = requests
        self.drain_calls = 0

    def drain_requests(self) -> tuple[InstanceRequest, ...]:
        self.drain_calls += 1
        requests, self.requests = self.requests, ()
        return requests


class _PumpRoot:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []

    def after(self, delay: int, callback: object) -> str:
        self.scheduled.append((delay, callback))
        return f"after-{len(self.scheduled)}"


class _PumpHarness(QuickAccessApp):
    def __init__(self, requests: tuple[InstanceRequest, ...]) -> None:
        self._stopping = False
        self._instance_guard = _RequestGuard(requests)
        self.bus = CommandBus()
        self.root = _PumpRoot()
        self.panel_calls = 0
        self.settings_calls = 0

    def open_panel(self, _cursor_position: tuple[int, int] | None = None) -> None:
        self.panel_calls += 1

    def open_settings(self) -> None:
        self.settings_calls += 1


class AppRuntimeIntegrationTests(unittest.TestCase):
    def test_settings_flag_is_explicit_and_does_not_imply_startup(self) -> None:
        args = _parse_args(["--settings"])

        self.assertTrue(args.settings)
        self.assertFalse(args.startup)
        self.assertFalse(args.smoke_test)

    def test_later_process_requests_panel_and_closes_its_guard(self) -> None:
        guard = MagicMock()
        guard.acquire.return_value = False
        hooks = MagicMock()

        with (
            patch("quickaccess.app.SingleInstanceGuard", return_value=guard),
            patch("quickaccess.app.configure_logging"),
            patch("quickaccess.app.install_exception_hooks", return_value=hooks),
            patch("quickaccess.app.close_logging"),
            patch("quickaccess.app.require_windows"),
            patch("quickaccess.app.ConfigStore") as store_type,
        ):
            result = main([])

        self.assertEqual(0, result)
        guard.notify_existing.assert_called_once_with(InstanceRequest.SHOW_PANEL)
        guard.close.assert_called_once_with()
        hooks.restore.assert_called_once_with()
        store_type.assert_not_called()

    def test_later_settings_launch_requests_resident_settings(self) -> None:
        guard = MagicMock()
        guard.acquire.return_value = False
        hooks = MagicMock()

        with (
            patch("quickaccess.app.SingleInstanceGuard", return_value=guard),
            patch("quickaccess.app.configure_logging"),
            patch("quickaccess.app.install_exception_hooks", return_value=hooks),
            patch("quickaccess.app.close_logging"),
            patch("quickaccess.app.require_windows"),
        ):
            result = main(["--settings"])

        self.assertEqual(0, result)
        guard.notify_existing.assert_called_once_with(InstanceRequest.OPEN_SETTINGS)
        guard.close.assert_called_once_with()

    def test_smoke_instance_keeps_its_separate_signal_namespace(self) -> None:
        guard = MagicMock()
        guard.acquire.return_value = False
        guard_type = MagicMock(return_value=guard)
        hooks = MagicMock()

        with (
            patch("quickaccess.app.SingleInstanceGuard", guard_type),
            patch("quickaccess.app.configure_logging"),
            patch("quickaccess.app.install_exception_hooks", return_value=hooks),
            patch("quickaccess.app.close_logging"),
            patch("quickaccess.app.require_windows"),
        ):
            result = main(["--smoke-test"])

        self.assertEqual(0, result)
        guard_type.assert_called_once_with(f"{MUTEX_NAME}-Smoke")
        guard.notify_existing.assert_called_once_with(InstanceRequest.SHOW_PANEL)

    def test_owner_guard_is_passed_to_app_and_settings_open_after_start(self) -> None:
        guard = MagicMock()
        guard.acquire.return_value = True
        hooks = MagicMock()
        config = LauncherConfig.default()
        load_result = SimpleNamespace(config=config)
        store = MagicMock()
        store.load.return_value = load_result
        root = MagicMock()
        application = MagicMock()
        app_type = MagicMock(return_value=application)

        with (
            patch("quickaccess.app.SingleInstanceGuard", return_value=guard),
            patch("quickaccess.app.configure_logging"),
            patch("quickaccess.app.install_exception_hooks", return_value=hooks),
            patch("quickaccess.app.close_logging"),
            patch("quickaccess.app.require_windows"),
            patch("quickaccess.app.enable_dpi_awareness"),
            patch("quickaccess.app.ConfigStore", return_value=store),
            patch("quickaccess.app.apply_appearance_mode"),
            patch("quickaccess.app.ctk.set_default_color_theme"),
            patch("quickaccess.app.ctk.CTk", return_value=root),
            patch("quickaccess.app.QuickAccessApp", app_type),
        ):
            result = main(["--settings"])

        self.assertEqual(0, result)
        app_type.assert_called_once_with(
            root,
            store,
            load_result,
            started_at_logon=False,
            smoke_test=False,
            instance_guard=guard,
        )
        application.start.assert_called_once_with()
        root.after.assert_called_once_with(0, application.open_settings)
        guard.close.assert_called_once_with()

    def test_owner_consumes_activation_events_on_existing_ui_pump(self) -> None:
        harness = _PumpHarness(
            (InstanceRequest.SHOW_PANEL, InstanceRequest.OPEN_SETTINGS)
        )

        harness._drain_commands()

        self.assertEqual(1, harness._instance_guard.drain_calls)
        self.assertEqual(1, harness.panel_calls)
        self.assertEqual(1, harness.settings_calls)
        self.assertEqual(1, len(harness.root.scheduled))

    def test_startup_change_rejects_unverified_post_state_and_keeps_config(self) -> None:
        startup = _ScriptedStartup(
            [
                _startup_status(StartupRegistrationState.STALE, True),
                _startup_status(StartupRegistrationState.ABSENT, False),
            ]
        )
        harness = _StartupControllerHarness(startup, desired=False)

        with patch(
            "quickaccess.app.startup_invocation",
            return_value=(r"C:\New\QuickAccess.exe", ("--startup",)),
        ):
            result = harness.set_startup(True)

        self.assertFalse(result)
        self.assertFalse(harness.config.run_on_startup)
        self.assertEqual([True, False], [call[0] for call in startup.reconcile_calls])
        self.assertEqual(StartupRegistrationState.ABSENT, harness._startup_status.state)

    def test_config_save_failure_rolls_windows_state_back_and_verifies_it(self) -> None:
        startup = _ScriptedStartup(
            [
                _startup_status(StartupRegistrationState.CORRECT, True),
                _startup_status(StartupRegistrationState.ABSENT, False),
            ]
        )
        harness = _StartupControllerHarness(
            startup,
            desired=False,
            commit_succeeds=False,
        )

        with patch(
            "quickaccess.app.startup_invocation",
            return_value=(r"C:\New\QuickAccess.exe", ("--startup",)),
        ):
            result = harness.set_startup(True)

        self.assertFalse(result)
        self.assertFalse(harness.config.run_on_startup)
        self.assertEqual([True, False], [call[0] for call in startup.reconcile_calls])
        self.assertTrue(harness._startup_status.in_sync)

    def test_normal_start_reconciles_current_executable_without_overwriting_intent(self) -> None:
        status = _startup_status(StartupRegistrationState.CORRECT, True)
        startup = _ScriptedStartup([status])
        harness = _StartupControllerHarness(startup, desired=True)

        with patch(
            "quickaccess.app.startup_invocation",
            return_value=(r"C:\New\QuickAccess.exe", ("--startup",)),
        ):
            harness._synchronize_startup_registration()

        self.assertEqual(
            [(True, r"C:\New\QuickAccess.exe", ("--startup",))],
            startup.reconcile_calls,
        )
        self.assertTrue(harness.config.run_on_startup)
        self.assertIs(status, harness._startup_status)

    def test_unreadable_startup_state_is_exposed_through_settings_actions(self) -> None:
        status = _startup_status(
            StartupRegistrationState.UNREADABLE,
            True,
            error="PermissionError: policy denied access",
        )
        startup = _ScriptedStartup([status])
        harness = _StartupControllerHarness(startup, desired=True)

        actions = harness._build_settings_actions()
        self.assertIsInstance(actions, SettingsActions)
        self.assertIsNotNone(actions.get_startup_status)
        actual = actions.get_startup_status()  # type: ignore[misc]

        self.assertIs(status, actual)
        self.assertEqual(StartupRegistrationState.UNREADABLE, actual.state)
        self.assertFalse(actual.in_sync)


if __name__ == "__main__":
    unittest.main()
