from __future__ import annotations

import sys
import unittest
import uuid

from quickaccess.services.monitor import Point, Rect, Size, clamp_window_to_work_area
from quickaccess.services.singleton import SingleInstanceGuard, local_mutex_name
from quickaccess.services.startup import RUN_KEY, StartupManager, build_startup_command


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

    def __init__(self) -> None:
        self.key_exists = False
        self.values: dict[str, str] = {}

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

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[str, int]:
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key: _FakeKey, name: str) -> None:
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

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
