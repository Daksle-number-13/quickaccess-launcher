from __future__ import annotations

import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quickaccess.app import QuickAccessApp
from quickaccess.commands import (
    CommandBus,
    CommandSource,
    LaunchResultCommand,
    OpenPanelCommand,
)
from quickaccess.models import LauncherConfig
from quickaccess.services.explorer import ExplorerTargetResult
from quickaccess.services.launcher import FileLauncher
from quickaccess.services.monitor import MonitorContext, Point, Rect
from quickaccess.services.startup import (
    StartupRegistrationState,
    StartupRegistrationStatus,
)
from quickaccess.services.update_check import UpdateCheckResult


class _PublishHarness:
    def __init__(self, config: LauncherConfig, launcher: FileLauncher) -> None:
        self.config = config
        self.launcher = launcher
        self.bus = CommandBus()
        self._launch_inflight: set[str] = set()
        self._launch_slots = threading.BoundedSemaphore(4)
        self.toast = _ToastRecorder()

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

    def reconcile(
        self, enabled: bool, _executable: str, _arguments: object
    ) -> StartupRegistrationStatus:
        self.calls += 1
        if self.calls == 2:
            raise OSError("registry rollback denied")
        return StartupRegistrationStatus(
            state=(
                StartupRegistrationState.CORRECT
                if enabled
                else StartupRegistrationState.ABSENT
            ),
            desired_enabled=enabled,
            expected_command="QuickAccess.exe --startup",
        )

    @staticmethod
    def _require_startup_in_sync(status: StartupRegistrationStatus) -> None:
        QuickAccessApp._require_startup_in_sync(status)

    def _rollback_startup_registration(
        self, desired_enabled: bool, executable: str, arguments: tuple[str, ...]
    ) -> bool:
        return QuickAccessApp._rollback_startup_registration(
            self, desired_enabled, executable, arguments  # type: ignore[arg-type]
        )

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
        self._popup_refresh_requires_layout = False

    @staticmethod
    def _flush_popup_refresh() -> None:
        pass


class _IconRequestScheduleHarness:
    def __init__(self) -> None:
        self.root = _AfterIdleRoot()
        self._stopping = False
        self._icon_request_after: str | None = None
        self.request_calls = 0

    def _request_all_icons(self) -> None:
        self.request_calls += 1

    def _flush_icon_requests(self) -> None:
        QuickAccessApp._flush_icon_requests(self)  # type: ignore[arg-type]


class _PooledPopup:
    def __init__(self) -> None:
        self.exists = True
        self.hidden = 0
        self.destroyed = 0

    def winfo_exists(self) -> bool:
        return self.exists

    def hide(self) -> None:
        self.hidden += 1

    def destroy(self) -> None:
        self.destroyed += 1
        self.exists = False


class _PopupPoolHarness:
    def __init__(self) -> None:
        self.popup = None
        self._popup_pool: dict[tuple[str, int | None], _PooledPopup] = {}
        self._popup_contexts: dict[
            tuple[str, int | None], MonitorContext
        ] = {}
        self.created: list[_PooledPopup] = []

    def _new_popup(self) -> _PooledPopup:
        popup = _PooledPopup()
        self.created.append(popup)
        return popup


class _MutableCursorMonitor:
    def __init__(self) -> None:
        self.position = Point(100, 100)
        self.reads = 0

    def get_cursor_position(self) -> Point:
        self.reads += 1
        return self.position

    @staticmethod
    def get_monitor_context(_point: Point) -> MonitorContext:
        desktop = Rect(0, 0, 1920, 1040)
        return MonitorContext("display-1", desktop, desktop, 1.0)


class _AnchorPopup:
    def __init__(self) -> None:
        self.anchors: list[Point] = []

    def show(self, *args: object, **_kwargs: object) -> None:
        self.anchors.append(args[2])  # type: ignore[arg-type]


class _CursorDispatchHarness:
    def __init__(self) -> None:
        self.bus = CommandBus()
        self.monitor = _MutableCursorMonitor()
        self.config = LauncherConfig.default()
        self.statuses: dict[str, object] = {}
        self.icon_images: dict[str, object] = {}
        self.popup = None
        self._popup = _AnchorPopup()
        self._last_anchor = None
        self._last_work_area = None
        self._last_monitor_context = None

    def _safe_publish(self, command: object) -> None:
        self.bus.publish(command)  # type: ignore[arg-type]

    def open_panel(self, cursor_position: tuple[int, int] | None = None) -> None:
        QuickAccessApp.open_panel(self, cursor_position)  # type: ignore[arg-type]

    def _monitor_context_at(self, anchor: Point) -> MonitorContext:
        return QuickAccessApp._monitor_context_at(self, anchor)  # type: ignore[arg-type]

    def _ensure_popup(self, _context: MonitorContext) -> _AnchorPopup:
        return self._popup

    @staticmethod
    def _schedule_icon_requests() -> None:
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
        self.check_calls = 0
        self._stopping = False
        self._update_check_after: str | None = None
        self._update_check_generation = 0
        self._update_check_inflight_generations: set[int] = set()
        self.cancelled_after: list[str] = []
        self.root = self

    def _commit(self, mutator: object, _message: str) -> bool:
        mutator(self.config)  # type: ignore[operator]
        return True

    def _check_for_update(self) -> None:
        self.check_calls += 1

    def after_cancel(self, after_id: str) -> None:
        self.cancelled_after.append(after_id)

    def _cancel_update_check_schedule(self) -> None:
        QuickAccessApp._cancel_update_check_schedule(self)  # type: ignore[arg-type]


class _QuickAddWatchdogHarness:
    def __init__(self) -> None:
        self._quick_add_inflight = True
        self._quick_add_generation = 7
        self._quick_add_timeout_after: str | None = "quick-add-timeout"
        self.toast = _ToastRecorder()


class _StartRootRecorder:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []
        self.cancelled: list[str] = []

    def after(self, delay: int, callback: object) -> str:
        self.scheduled.append((delay, callback))
        return f"after-{len(self.scheduled)}"

    def after_idle(self, callback: object) -> str:
        self.scheduled.append((-1, callback))
        return f"idle-{len(self.scheduled)}"

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.append(after_id)


class _ReadyTray:
    last_error = None

    @staticmethod
    def start() -> bool:
        return True

    @staticmethod
    def wait_until_ready(*, timeout: float) -> bool:
        return timeout > 0


class _StartHarness:
    def __init__(self, *, check_updates: bool, smoke_test: bool = False) -> None:
        self.config = LauncherConfig.default()
        self.config.check_updates = check_updates
        self.smoke_test = smoke_test
        self.root = _StartRootRecorder()
        self.tray = _ReadyTray()
        self.toast = _ToastRecorder()
        self.load_result = SimpleNamespace(
            recovered=False,
            repaired=False,
            created=False,
            backup_path=None,
        )
        self.startup_sync_calls = 0
        self.update_check_calls = 0
        self.start_events: list[str] = []
        self._update_check_after: str | None = None
        self._update_check_generation = 0
        self._update_check_inflight_generations: set[int] = set()

    def _synchronize_startup_registration(self) -> None:
        self.startup_sync_calls += 1

    def _configure_hotkeys(
        self, _panel: str, _quick_add: str, *, show_error: bool
    ) -> bool:
        self.start_events.append("hotkeys")
        return show_error

    @staticmethod
    def _drain_commands() -> None:
        pass

    @staticmethod
    def _validate_all_paths() -> None:
        pass

    @staticmethod
    def _request_all_icons() -> None:
        pass

    def _prewarm_popup(self) -> None:
        self.start_events.append("prewarm")

    @staticmethod
    def _schedule_monitor_refresh() -> None:
        pass

    def _check_for_update(self) -> None:
        self.update_check_calls += 1

    def _schedule_update_check(self, delay_ms: int) -> None:
        QuickAccessApp._schedule_update_check(self, delay_ms)  # type: ignore[arg-type]

    def _run_scheduled_update_check(self) -> None:
        QuickAccessApp._run_scheduled_update_check(self)  # type: ignore[arg-type]

    def _cancel_update_check_schedule(self) -> None:
        QuickAccessApp._cancel_update_check_schedule(self)  # type: ignore[arg-type]


def _run_scheduled_callback(harness: _StartHarness, callback_name: str) -> bool:
    for _delay, callback in harness.root.scheduled:
        if getattr(callback, "__name__", "") == callback_name:
            callback()  # type: ignore[operator]
            return True
    return False


class ControllerResponsivenessTests(unittest.TestCase):
    def test_hotkey_panel_uses_cursor_at_ui_dispatch_not_callback_time(self) -> None:
        harness = _CursorDispatchHarness()

        QuickAccessApp._hotkey_open_panel(harness)  # type: ignore[arg-type]

        self.assertEqual(0, harness.monitor.reads)
        command = harness.bus.get_nowait()
        self.assertIsInstance(command, OpenPanelCommand)
        assert isinstance(command, OpenPanelCommand)
        self.assertIs(command.source, CommandSource.HOTKEY)
        self.assertIsNone(command.cursor_position)

        harness.monitor.position = Point(900, 600)
        QuickAccessApp._handle_command(harness, command)  # type: ignore[arg-type]

        self.assertEqual(1, harness.monitor.reads)
        self.assertEqual([Point(900, 600)], harness._popup.anchors)
        self.assertEqual(Point(900, 600), harness._last_anchor)

    def test_icon_retry_requests_are_deferred_and_coalesced(self) -> None:
        harness = _IconRequestScheduleHarness()

        QuickAccessApp._schedule_icon_requests(harness)  # type: ignore[arg-type]
        QuickAccessApp._schedule_icon_requests(harness)  # type: ignore[arg-type]

        self.assertEqual(1, len(harness.root.callbacks))
        callback = harness.root.callbacks[0]
        callback()  # type: ignore[operator]
        self.assertEqual(1, harness.request_calls)
        self.assertIsNone(harness._icon_request_after)

    def test_popup_pool_reuses_one_prepared_window_per_monitor(self) -> None:
        harness = _PopupPoolHarness()
        left = MonitorContext(
            r"\\.\DISPLAY1",
            Rect(-1080, 0, 0, 1920),
            Rect(-1080, 0, 0, 1872),
            1.0,
        )
        right = MonitorContext(
            r"\\.\DISPLAY2",
            Rect(0, 0, 2880, 1800),
            Rect(0, 0, 2880, 1704),
            2.0,
        )

        first_left = QuickAccessApp._ensure_popup(harness, left)  # type: ignore[arg-type]
        second_left = QuickAccessApp._ensure_popup(harness, left)  # type: ignore[arg-type]
        first_right = QuickAccessApp._ensure_popup(harness, right)  # type: ignore[arg-type]

        self.assertIs(first_left, second_left)
        self.assertIsNot(first_left, first_right)
        self.assertEqual(2, len(harness.created))

    def test_popup_pool_retires_old_window_when_monitor_scale_changes(self) -> None:
        harness = _PopupPoolHarness()
        at_150 = MonitorContext(
            r"\\.\DISPLAY2",
            Rect(0, 0, 3840, 2160),
            Rect(0, 0, 3840, 2088),
            1.5,
        )
        at_200 = MonitorContext(
            r"\\.\DISPLAY2",
            Rect(0, 0, 3840, 2160),
            Rect(0, 0, 3840, 2088),
            2.0,
        )

        old_popup = QuickAccessApp._ensure_popup(harness, at_150)  # type: ignore[arg-type]
        harness.popup = old_popup
        new_popup = QuickAccessApp._ensure_popup(harness, at_200)  # type: ignore[arg-type]

        self.assertIsNot(old_popup, new_popup)
        self.assertEqual(1, old_popup.hidden)
        self.assertEqual(1, old_popup.destroyed)
        self.assertIsNone(harness.popup)
        self.assertEqual({at_200.cache_key}, set(harness._popup_pool))

    def test_monitor_poll_prewarms_new_topology_before_next_hotkey(self) -> None:
        contexts = (
            MonitorContext(
                r"\\.\DISPLAY1",
                Rect(0, 0, 1920, 1080),
                Rect(0, 0, 1920, 1040),
                1.0,
            ),
            MonitorContext(
                r"\\.\DISPLAY2",
                Rect(1920, 0, 4480, 1440),
                Rect(1920, 0, 4480, 1400),
                1.5,
            ),
        )
        prepared: list[tuple[str, float | None]] = []
        discarded: list[set[tuple[str, int | None]]] = []
        scheduled: list[bool] = []

        def popup_for(context: MonitorContext) -> object:
            return SimpleNamespace(
                prepare=lambda *_args, **kwargs: prepared.append(
                    (context.identifier, kwargs.get("target_dpi_scale"))
                )
            )

        harness = SimpleNamespace(
            _monitor_refresh_after="old-timer",
            _stopping=False,
            monitor=SimpleNamespace(get_monitor_contexts=lambda: contexts),
            _monitor_topology_signature=(),
            config=LauncherConfig.default(),
            statuses={},
            icon_images={},
            _topology_signature=lambda values: QuickAccessApp._topology_signature(values),
            _ensure_popup=popup_for,
            _discard_stale_popups=lambda keys: discarded.append(keys),
            _schedule_monitor_refresh=lambda: scheduled.append(True),
        )

        QuickAccessApp._poll_monitor_topology(harness)  # type: ignore[arg-type]

        self.assertEqual(
            [(contexts[0].identifier, 1.0), (contexts[1].identifier, 1.5)],
            prepared,
        )
        self.assertEqual([{context.cache_key for context in contexts}], discarded)
        self.assertEqual(
            QuickAccessApp._topology_signature(contexts),
            harness._monitor_topology_signature,
        )
        self.assertEqual([True], scheduled)

    def test_start_prewarms_popup_before_enabling_hotkeys(self) -> None:
        harness = _StartHarness(check_updates=False)

        QuickAccessApp.start(harness)  # type: ignore[arg-type]

        self.assertEqual(["prewarm", "hotkeys"], harness.start_events)

    def test_start_always_reconciles_startup_without_network_opt_in(self) -> None:
        harness = _StartHarness(check_updates=False)

        QuickAccessApp.start(harness)  # type: ignore[arg-type]

        self.assertEqual(1, harness.startup_sync_calls)
        self.assertFalse(_run_scheduled_callback(harness, "_run_scheduled_update_check"))
        self.assertEqual(0, harness.update_check_calls)

    def test_start_schedules_update_check_only_after_explicit_opt_in(self) -> None:
        harness = _StartHarness(check_updates=True)

        QuickAccessApp.start(harness)  # type: ignore[arg-type]

        self.assertEqual(1, harness.startup_sync_calls)
        self.assertTrue(_run_scheduled_callback(harness, "_run_scheduled_update_check"))
        self.assertEqual(1, harness.update_check_calls)

    def test_smoke_start_changes_neither_startup_registry_nor_network(self) -> None:
        harness = _StartHarness(check_updates=True, smoke_test=True)

        QuickAccessApp.start(harness)  # type: ignore[arg-type]

        self.assertEqual(0, harness.startup_sync_calls)
        self.assertFalse(_run_scheduled_callback(harness, "_run_scheduled_update_check"))
        self.assertEqual(0, harness.update_check_calls)

    def test_update_checks_require_explicit_opt_in(self) -> None:
        harness = _UpdateCheckHarness()
        self.assertFalse(harness.config.check_updates)

        self.assertTrue(
            QuickAccessApp.set_update_checks(harness, True)  # type: ignore[arg-type]
        )
        self.assertTrue(harness.config.check_updates)
        self.assertEqual(1, harness.check_calls)

        self.assertTrue(
            QuickAccessApp.set_update_checks(harness, False)  # type: ignore[arg-type]
        )
        self.assertFalse(harness.config.check_updates)
        self.assertEqual(1, harness.check_calls)

    def test_update_opt_out_cancels_schedule_and_ignores_stale_worker_result(self) -> None:
        harness = _UpdateCheckHarness()
        harness.config.check_updates = True
        harness._update_check_after = "scheduled-check"
        harness._update_check_inflight_generations.add(0)

        self.assertTrue(
            QuickAccessApp.set_update_checks(harness, False)  # type: ignore[arg-type]
        )
        self.assertEqual(["scheduled-check"], harness.cancelled_after)
        self.assertEqual(1, harness._update_check_generation)

        QuickAccessApp._apply_update_check(  # type: ignore[arg-type]
            harness,
            (
                0,
                UpdateCheckResult(
                    available=True,
                    latest_version="v9.9.9",
                    release_url="https://example.invalid/releases/v9.9.9",
                ),
            ),
        )

        self.assertEqual("", harness.config.last_update_notice)
        self.assertEqual([], harness.toast.messages)
        self.assertNotIn(0, harness._update_check_inflight_generations)

    def test_update_result_from_previous_opt_in_period_stays_stale_after_reenable(
        self,
    ) -> None:
        harness = _UpdateCheckHarness()
        harness.config.check_updates = True
        harness._update_check_generation = 4
        harness._update_check_inflight_generations.add(3)

        QuickAccessApp._apply_update_check(  # type: ignore[arg-type]
            harness,
            (3, UpdateCheckResult(available=True, latest_version="v9.9.9")),
        )

        self.assertEqual("", harness.config.last_update_notice)
        self.assertEqual([], harness.toast.messages)

    def test_update_check_deduplicates_workers_within_one_generation(self) -> None:
        harness = _UpdateCheckHarness()
        harness.config.check_updates = True
        harness._safe_publish = lambda _command: None  # type: ignore[attr-defined]

        with patch("quickaccess.app.threading.Thread") as worker_thread:
            QuickAccessApp._check_for_update(harness)  # type: ignore[arg-type]
            QuickAccessApp._check_for_update(harness)  # type: ignore[arg-type]

        worker_thread.assert_called_once()
        worker_thread.return_value.start.assert_called_once_with()

    def test_visible_popup_refreshes_are_coalesced_until_idle(self) -> None:
        harness = _PopupRefreshHarness()

        QuickAccessApp._refresh_visible_popup(harness)  # type: ignore[arg-type]
        QuickAccessApp._refresh_visible_popup(  # type: ignore[arg-type]
            harness,
            layout_required=True,
        )

        self.assertEqual(len(harness.root.callbacks), 1)
        self.assertEqual(harness._popup_refresh_after, "after-1")
        self.assertTrue(harness._popup_refresh_requires_layout)

    def test_hidden_popup_refresh_is_applied_during_prewarm(self) -> None:
        prewarm_calls: list[bool] = []
        harness = SimpleNamespace(
            _popup_refresh_after="after-1",
            _popup_refresh_requires_layout=False,
            _stopping=False,
            popup=SimpleNamespace(visible=False),
            _popup_pool={},
            _prewarm_popup=lambda: prewarm_calls.append(True),
        )

        QuickAccessApp._flush_popup_refresh(harness)  # type: ignore[arg-type]

        self.assertIsNone(harness._popup_refresh_after)
        self.assertEqual([True], prewarm_calls)

    def test_activate_item_does_not_block_caller_when_shell_launch_stalls(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocking_startfile(_path: str) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(2.0)

        config = LauncherConfig(items=[])
        item = config.add_item(r"\\server\offline\문서.xlsx", name="네트워크 문서")
        harness = _PublishHarness(config, FileLauncher(blocking_startfile))

        started = time.monotonic()
        QuickAccessApp.activate_item(harness, item.id)  # type: ignore[arg-type]
        QuickAccessApp.activate_item(harness, item.id)  # type: ignore[arg-type]
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)
        self.assertTrue(entered.wait(1.0))
        self.assertEqual(1, calls)
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
        assert isinstance(command, LaunchResultCommand)
        QuickAccessApp._finish_launch(  # type: ignore[arg-type]
            harness, command.item_name, command.result
        )
        self.assertNotIn(item.id, harness._launch_inflight)

    def test_launch_workers_are_bounded_when_multiple_shell_calls_stall(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        launched: list[str] = []

        def blocking_startfile(path: str) -> None:
            launched.append(path)
            entered.set()
            release.wait(2.0)

        config = LauncherConfig(items=[])
        first = config.add_item(r"\\server\offline-one", name="첫 번째")
        second = config.add_item(r"\\server\offline-two", name="두 번째")
        harness = _PublishHarness(config, FileLauncher(blocking_startfile))
        harness._launch_slots = threading.BoundedSemaphore(1)

        QuickAccessApp.activate_item(harness, first.id)  # type: ignore[arg-type]
        self.assertTrue(entered.wait(1.0))
        QuickAccessApp.activate_item(harness, second.id)  # type: ignore[arg-type]

        self.assertEqual([first.path], launched)
        self.assertIn("잠시 후", harness.toast.messages[-1][0])
        self.assertNotIn(second.id, harness._launch_inflight)
        release.set()

    def test_quick_add_watchdog_releases_feature_and_ignores_late_result(self) -> None:
        harness = _QuickAddWatchdogHarness()

        QuickAccessApp._expire_quick_add(harness, 7)  # type: ignore[arg-type]

        self.assertFalse(harness._quick_add_inflight)
        self.assertEqual(8, harness._quick_add_generation)
        self.assertIn("중단했습니다", harness.toast.messages[-1][0])
        late_result = ExplorerTargetResult(
            success=True,
            path=r"C:\Late\item.txt",
            suggested_name="item.txt",
            item_type="file",
        )
        with patch("quickaccess.app.ask_display_name") as ask_name:
            QuickAccessApp._finish_quick_add(  # type: ignore[arg-type]
                harness, (7, late_result)
            )
        ask_name.assert_not_called()

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
        harness.config.check_updates = True
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
        harness.config.check_updates = True
        harness.config.last_update_notice = "v9.9.9"
        result = UpdateCheckResult(available=True, latest_version="v9.9.9")

        QuickAccessApp._apply_update_check(harness, result)  # type: ignore[arg-type]

        self.assertEqual([], harness.toast.messages)

    def test_unavailable_update_result_shows_no_toast(self) -> None:
        harness = _UpdateCheckHarness()
        harness.config.check_updates = True

        QuickAccessApp._apply_update_check(  # type: ignore[arg-type]
            harness, UpdateCheckResult(available=False)
        )

        self.assertEqual([], harness.toast.messages)


if __name__ == "__main__":
    unittest.main()
