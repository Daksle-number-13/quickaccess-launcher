from __future__ import annotations

import sys
import unittest
import uuid

from quickaccess.services.monitor import (
    NativeMonitorService,
    Point,
    Rect,
    Size,
    clamp_window_to_work_area,
)
from quickaccess.services.singleton import SingleInstanceGuard, local_mutex_name
from quickaccess.services.startup import (
    RUN_KEY,
    StartupManager,
    StartupRegistrationState,
    build_startup_command,
    startup_command_matches,
)


class MonitorGeometryTests(unittest.TestCase):
    def test_position_is_unchanged_when_popup_fits(self) -> None:
        result = clamp_window_to_work_area(
            Point(100, 200), Size(300, 250), Rect(0, 0, 1920, 1040)
        )
        self.assertEqual(result, Point(100, 200))

    def test_position_clamps_right_and_bottom_edges(self) -> None:
        result = clamp_window_to_work_area(
            Point(1850, 1000), Size(300, 250), Rect(0, 0, 1920, 1040)
        )
        self.assertEqual(result, Point(1620, 790))

    def test_negative_monitor_coordinates_are_preserved(self) -> None:
        result = clamp_window_to_work_area(
            Point(-50, -20), Size(400, 300), Rect(-1920, -200, 0, 880)
        )
        self.assertEqual(result, Point(-400, -20))

    def test_oversized_popup_anchors_to_work_area_origin(self) -> None:
        result = clamp_window_to_work_area(
            Point(500, 400), Size(1200, 900), Rect(100, 50, 900, 650)
        )
        self.assertEqual(result, Point(100, 50))


class _FakeMonitorApi:
    def __init__(self) -> None:
        self.monitors = {
            101: {
                "device": r"\\.\DISPLAY1",
                "bounds": (0, 0, 2880, 1800),
                "work": (0, 0, 2880, 1704),
            },
            202: {
                "device": r"\\.\DISPLAY2",
                "bounds": (-1080, 0, 0, 1920),
                "work": (-1080, 0, 0, 1872),
            },
        }

    @staticmethod
    def GetCursorPos(point: object) -> bool:
        point._obj.x = 10
        point._obj.y = 20
        return True

    @staticmethod
    def MonitorFromPoint(point: object, _flags: int) -> int:
        return 202 if point.x < 0 else 101

    def GetMonitorInfoW(self, monitor: int, info_pointer: object) -> bool:
        source = self.monitors[int(monitor)]
        info = info_pointer._obj
        (
            info.rcMonitor.left,
            info.rcMonitor.top,
            info.rcMonitor.right,
            info.rcMonitor.bottom,
        ) = source["bounds"]
        (
            info.rcWork.left,
            info.rcWork.top,
            info.rcWork.right,
            info.rcWork.bottom,
        ) = source["work"]
        info.szDevice = source["device"]
        return True

    def EnumDisplayMonitors(
        self,
        _device_context: object,
        _clip: object,
        callback: object,
        _user_data: int,
    ) -> bool:
        # Return primary first; the service must sort by desktop coordinates.
        for monitor in (101, 202):
            callback(monitor, None, None, 0)
        return True


class _FakeScaleApi:
    def __init__(self, factors: dict[int, int], *, result: int = 0) -> None:
        self.factors = factors
        self.result = result

    def GetScaleFactorForMonitor(self, monitor: int, factor: object) -> int:
        factor._obj.value = self.factors[int(monitor)]
        return self.result


class MonitorContextTests(unittest.TestCase):
    def test_context_combines_device_work_area_and_scale_before_show(self) -> None:
        service = NativeMonitorService(
            _FakeMonitorApi(),
            _FakeScaleApi({101: 200, 202: 100}),
        )

        context = service.get_monitor_context(Point(-500, 100))

        self.assertEqual(r"\\.\DISPLAY2", context.identifier)
        self.assertEqual(Rect(-1080, 0, 0, 1920), context.bounds)
        self.assertEqual(Rect(-1080, 0, 0, 1872), context.work_area)
        self.assertEqual(1.0, context.scale)

    def test_monitor_enumeration_is_sorted_and_preserves_mixed_dpi(self) -> None:
        service = NativeMonitorService(
            _FakeMonitorApi(),
            _FakeScaleApi({101: 200, 202: 100}),
        )

        contexts = service.get_monitor_contexts()

        self.assertEqual(
            [r"\\.\DISPLAY2", r"\\.\DISPLAY1"],
            [context.identifier for context in contexts],
        )
        self.assertEqual([1.0, 2.0], [context.scale for context in contexts])

    def test_scale_api_failure_keeps_usable_monitor_geometry(self) -> None:
        service = NativeMonitorService(
            _FakeMonitorApi(),
            _FakeScaleApi({101: 200, 202: 100}, result=-1),
        )

        context = service.get_monitor_context(Point(10, 10))

        self.assertEqual(Rect(0, 0, 2880, 1704), context.work_area)
        self.assertIsNone(context.scale)


class _FakeKey:
    def __init__(self, registry: "_FakeRegistry") -> None:
        self.registry = registry

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_BINARY = 3

    def __init__(self) -> None:
        self.key_exists = False
        self.values: dict[str, str] = {}
        self.value_types: dict[str, int] = {}
        self.query_error: Exception | None = None
        self.write_count = 0
        self.delete_count = 0

    def CreateKeyEx(self, root: object, path: str, reserved: int, access: int) -> _FakeKey:
        self.assert_path(path)
        self.key_exists = True
        return _FakeKey(self)

    def OpenKey(self, root: object, path: str, reserved: int, access: int) -> _FakeKey:
        self.assert_path(path)
        if not self.key_exists:
            raise FileNotFoundError(path)
        return _FakeKey(self)

    def SetValueEx(
        self, key: _FakeKey, name: str, reserved: int, value_type: int, value: str
    ) -> None:
        self.values[name] = value
        self.value_types[name] = value_type
        self.write_count += 1

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[str, int]:
        if self.query_error is not None:
            raise self.query_error
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.value_types.get(name, self.REG_SZ)

    def DeleteValue(self, key: _FakeKey, name: str) -> None:
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]
        self.value_types.pop(name, None)
        self.delete_count += 1

    def assert_path(self, path: str) -> None:
        if path != RUN_KEY:
            raise AssertionError(path)


class StartupTests(unittest.TestCase):
    def test_command_builder_quotes_executable_and_arguments(self) -> None:
        command = build_startup_command(
            r"C:\Program Files\QuickAccess\QuickAccess.exe",
            ("--label", "품질 팀"),
        )
        self.assertEqual(
            command,
            '"C:\\Program Files\\QuickAccess\\QuickAccess.exe" --label "품질 팀"',
        )

    def test_manager_only_mutates_its_own_run_value(self) -> None:
        registry = _FakeRegistry()
        registry.key_exists = True
        registry.values["OtherApplication"] = "other.exe"
        manager = StartupManager("QuickAccessTest", registry=registry)

        expected = manager.enable(r"C:\Quick Access\QuickAccess.exe")
        self.assertEqual(manager.get_command(), expected)
        self.assertTrue(manager.is_enabled(r"C:\Quick Access\QuickAccess.exe"))
        self.assertTrue(manager.disable())
        self.assertEqual(registry.values, {"OtherApplication": "other.exe"})
        self.assertFalse(manager.disable())

    def test_command_match_accepts_equivalent_windows_path_spellings(self) -> None:
        self.assertTrue(
            startup_command_matches(
                r'"%localappdata%/QuickAccess/./QUICKACCESS.exe" --startup',
                r"C:\Users\Jinsu\AppData\Local\QuickAccess\QuickAccess.exe",
                ("--startup",),
                environment={
                    "LOCALAPPDATA": r"C:\Users\Jinsu\AppData\Local",
                },
            )
        )
        self.assertTrue(
            startup_command_matches(
                build_startup_command(
                    r"C:\Quick Access\QuickAccess.exe",
                    ("--label", '품질 "A" 팀'),
                ),
                r"C:\Quick Access\QuickAccess.exe",
                ("--label", '품질 "A" 팀'),
            )
        )

    def test_command_match_rejects_wrong_or_extra_startup_arguments(self) -> None:
        executable = r"C:\QuickAccess\QuickAccess.exe"
        self.assertFalse(startup_command_matches(executable, executable, ("--startup",)))
        self.assertFalse(
            startup_command_matches(
                f'"{executable}" --startup --unexpected',
                executable,
                ("--startup",),
            )
        )
        self.assertFalse(
            startup_command_matches(
                f'"{executable}" --STARTUP',
                executable,
                ("--startup",),
            )
        )

    def test_inspection_distinguishes_absent_state_from_desired_preference(self) -> None:
        registry = _FakeRegistry()
        manager = StartupManager("QuickAccessTest", registry=registry)
        executable = r"C:\QuickAccess\QuickAccess.exe"

        wanted = manager.inspect(True, executable, ("--startup",))
        disabled = manager.inspect(False, executable, ("--startup",))

        self.assertEqual(StartupRegistrationState.ABSENT, wanted.state)
        self.assertFalse(wanted.registered)
        self.assertFalse(wanted.in_sync)
        self.assertTrue(wanted.repairable)
        self.assertEqual(StartupRegistrationState.ABSENT, disabled.state)
        self.assertTrue(disabled.in_sync)
        self.assertFalse(disabled.repairable)

    def test_inspection_accepts_expand_string_and_normalized_command(self) -> None:
        registry = _FakeRegistry()
        registry.key_exists = True
        registry.values["QuickAccessTest"] = (
            r'"%localappdata%/QuickAccess/QUICKACCESS.exe" --startup'
        )
        registry.value_types["QuickAccessTest"] = registry.REG_EXPAND_SZ
        manager = StartupManager(
            "QuickAccessTest",
            registry=registry,
            environment={"LOCALAPPDATA": r"C:\Users\Jinsu\AppData\Local"},
        )

        status = manager.inspect(
            True,
            r"C:\Users\Jinsu\AppData\Local\QuickAccess\QuickAccess.exe",
            ("--startup",),
        )

        self.assertEqual(StartupRegistrationState.CORRECT, status.state)
        self.assertTrue(status.registered)
        self.assertTrue(status.in_sync)
        self.assertIsNone(status.error)

    def test_inspection_reports_stale_command_and_unsupported_value_type(self) -> None:
        registry = _FakeRegistry()
        registry.key_exists = True
        registry.values["QuickAccessTest"] = r'"C:\Old\QuickAccess.exe" --startup'
        manager = StartupManager("QuickAccessTest", registry=registry)
        executable = r"C:\New\QuickAccess.exe"

        stale = manager.inspect(True, executable, ("--startup",))
        self.assertEqual(StartupRegistrationState.STALE, stale.state)
        self.assertTrue(stale.registered)
        self.assertFalse(stale.in_sync)
        self.assertEqual(registry.values["QuickAccessTest"], stale.actual_command)

        registry.values["QuickAccessTest"] = build_startup_command(
            executable, ("--startup",)
        )
        registry.value_types["QuickAccessTest"] = registry.REG_BINARY
        unsupported = manager.inspect(True, executable, ("--startup",))
        self.assertEqual(StartupRegistrationState.STALE, unsupported.state)

    def test_inspection_reports_registry_read_failure_without_raising(self) -> None:
        registry = _FakeRegistry()
        registry.key_exists = True
        registry.query_error = PermissionError("policy denied access")
        manager = StartupManager("QuickAccessTest", registry=registry)

        status = manager.inspect(
            True,
            r"C:\QuickAccess\QuickAccess.exe",
            ("--startup",),
        )

        self.assertEqual(StartupRegistrationState.UNREADABLE, status.state)
        self.assertFalse(status.registered)
        self.assertFalse(status.in_sync)
        self.assertEqual("PermissionError: policy denied access", status.error)

    def test_reconcile_repairs_stale_entry_and_avoids_redundant_write(self) -> None:
        registry = _FakeRegistry()
        registry.key_exists = True
        registry.values["QuickAccessTest"] = r'"C:\Old\QuickAccess.exe" --startup'
        registry.values["OtherApplication"] = "other.exe"
        manager = StartupManager("QuickAccessTest", registry=registry)
        executable = r"C:\Quick Access\QuickAccess.exe"

        repaired = manager.reconcile(True, executable, ("--startup",))
        unchanged = manager.reconcile(True, executable, ("--startup",))

        self.assertEqual(StartupRegistrationState.CORRECT, repaired.state)
        self.assertTrue(repaired.in_sync)
        self.assertEqual(StartupRegistrationState.CORRECT, unchanged.state)
        self.assertEqual(1, registry.write_count)
        self.assertEqual("other.exe", registry.values["OtherApplication"])

    def test_reconcile_disabled_removes_correct_or_stale_owned_entry(self) -> None:
        for registered_command in (
            r'"C:\QuickAccess\QuickAccess.exe" --startup',
            r'"C:\Old\QuickAccess.exe" --startup',
        ):
            with self.subTest(registered_command=registered_command):
                registry = _FakeRegistry()
                registry.key_exists = True
                registry.values["QuickAccessTest"] = registered_command
                registry.values["OtherApplication"] = "other.exe"
                manager = StartupManager("QuickAccessTest", registry=registry)

                result = manager.reconcile(
                    False,
                    r"C:\QuickAccess\QuickAccess.exe",
                    ("--startup",),
                )

                self.assertEqual(StartupRegistrationState.ABSENT, result.state)
                self.assertTrue(result.in_sync)
                self.assertEqual(1, registry.delete_count)
                self.assertEqual(
                    {"OtherApplication": "other.exe"},
                    registry.values,
                )


class SingletonTests(unittest.TestCase):
    def test_name_is_forced_into_local_namespace(self) -> None:
        self.assertEqual(local_mutex_name("QuickAccess"), r"Local\QuickAccess")
        self.assertEqual(local_mutex_name(r"Local\QuickAccess"), r"Local\QuickAccess")
        with self.assertRaises(ValueError):
            local_mutex_name(r"Global\QuickAccess")

    @unittest.skipUnless(sys.platform == "win32", "native Windows mutex test")
    def test_second_guard_detects_existing_mutex(self) -> None:
        name = f"QuickAccessTest-{uuid.uuid4()}"
        first = SingleInstanceGuard(name)
        second = SingleInstanceGuard(name)
        third = SingleInstanceGuard(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue(second.already_running)
            first.close()
            self.assertTrue(third.acquire())
        finally:
            first.close()
            second.close()
            third.close()


if __name__ == "__main__":
    unittest.main()
