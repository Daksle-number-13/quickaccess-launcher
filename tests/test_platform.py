from __future__ import annotations

import unittest
from unittest import mock

import quickaccess.platform as qa_platform


class FakeFunction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class FakeLibrary:
    pass


class PlatformTests(unittest.TestCase):
    def test_windows_guard(self) -> None:
        with mock.patch.object(qa_platform.sys, "platform", "linux"):
            self.assertFalse(qa_platform.is_windows())
            with self.assertRaises(qa_platform.UnsupportedPlatformError):
                qa_platform.require_windows()

    def test_dpi_returns_unsupported_off_windows(self) -> None:
        with mock.patch.object(qa_platform.sys, "platform", "darwin"):
            result = qa_platform.enable_dpi_awareness()
        self.assertEqual(result.mode, qa_platform.DpiAwarenessMode.UNSUPPORTED)
        self.assertFalse(result.applied)

    def test_dpi_prefers_per_monitor_v2(self) -> None:
        user32 = FakeLibrary()
        user32.SetProcessDpiAwarenessContext = FakeFunction(True)

        with (
            mock.patch.object(qa_platform.sys, "platform", "win32"),
            mock.patch.object(qa_platform.ctypes, "WinDLL", return_value=user32),
            mock.patch.object(qa_platform.ctypes, "set_last_error"),
        ):
            result = qa_platform.enable_dpi_awareness()

        self.assertEqual(result.mode, qa_platform.DpiAwarenessMode.PER_MONITOR_V2)
        self.assertTrue(result.applied)
        self.assertEqual(len(user32.SetProcessDpiAwarenessContext.calls), 1)

    def test_dpi_falls_back_to_shcore(self) -> None:
        user32 = FakeLibrary()
        user32.SetProcessDpiAwarenessContext = FakeFunction(False)
        user32.SetProcessDPIAware = FakeFunction(False)
        shcore = FakeLibrary()
        shcore.SetProcessDpiAwareness = FakeFunction(0)

        def load_library(name, **_kwargs):
            return user32 if name == "user32" else shcore

        with (
            mock.patch.object(qa_platform.sys, "platform", "win32"),
            mock.patch.object(qa_platform.ctypes, "WinDLL", side_effect=load_library),
            mock.patch.object(qa_platform.ctypes, "set_last_error"),
            mock.patch.object(qa_platform.ctypes, "get_last_error", return_value=0),
        ):
            result = qa_platform.enable_dpi_awareness()

        self.assertEqual(result.mode, qa_platform.DpiAwarenessMode.PER_MONITOR)
        self.assertTrue(result.applied)

    def test_access_denied_means_manifest_or_library_configured_dpi(self) -> None:
        user32 = FakeLibrary()
        user32.SetProcessDpiAwarenessContext = FakeFunction(False)

        with (
            mock.patch.object(qa_platform.sys, "platform", "win32"),
            mock.patch.object(qa_platform.ctypes, "WinDLL", return_value=user32),
            mock.patch.object(qa_platform.ctypes, "set_last_error"),
            mock.patch.object(
                qa_platform.ctypes,
                "get_last_error",
                return_value=qa_platform.ERROR_ACCESS_DENIED,
            ),
        ):
            result = qa_platform.enable_dpi_awareness()

        self.assertEqual(
            result.mode, qa_platform.DpiAwarenessMode.ALREADY_CONFIGURED
        )
        self.assertTrue(result.applied)


if __name__ == "__main__":
    unittest.main()
