from __future__ import annotations

import threading
import time
import unittest

from quickaccess.services.icons import IconImage, IconService, icon_key


class _FakeBitmap:
    def __init__(self) -> None:
        self._info = {"bmWidth": 0, "bmHeight": 0}
        self.handle = object()

    def CreateCompatibleBitmap(self, _dc: object, width: int, height: int) -> None:
        self._info = {"bmWidth": width, "bmHeight": height}

    def GetInfo(self) -> dict[str, int]:
        return self._info

    def GetBitmapBits(self, _as_string: bool) -> bytes:
        pixels = self._info["bmWidth"] * self._info["bmHeight"]
        return b"\x11\x22\x33\xff" * pixels

    def GetHandle(self) -> object:
        return self.handle


class _FakeCompatibleDC:
    def __init__(self) -> None:
        self.selected: object = None
        self.drawn: list[tuple[object, object]] = []

    def SelectObject(self, bitmap: object) -> None:
        self.selected = bitmap

    def DrawIcon(self, position: tuple[int, int], hicon: object) -> None:
        self.drawn.append((position, hicon))

    def DeleteDC(self) -> None:
        pass


class _FakeSourceDC:
    def __init__(self) -> None:
        self.compatible_dc = _FakeCompatibleDC()

    def CreateCompatibleDC(self) -> _FakeCompatibleDC:
        return self.compatible_dc

    def DeleteDC(self) -> None:
        pass


class _FakeWin32Ui:
    def __init__(self) -> None:
        self.source_dc = _FakeSourceDC()

    def CreateDCFromHandle(self, _screen_dc: object) -> _FakeSourceDC:
        return self.source_dc

    def CreateBitmap(self) -> _FakeBitmap:
        return _FakeBitmap()


class _FakeWin32Gui:
    def __init__(self, hicon: int = 12345, raises: Exception | None = None) -> None:
        self.hicon = hicon
        self.raises = raises
        self.sh_get_file_info_calls: list[str] = []
        self.destroyed: list[object] = []

    def SHGetFileInfo(self, path: str, _attrs: int, _flags: int) -> tuple:
        self.sh_get_file_info_calls.append(path)
        if self.raises is not None:
            raise self.raises
        return (self.hicon, 0, 0, "", "")

    def GetDC(self, _hwnd: int) -> str:
        return "screen-dc"

    def ReleaseDC(self, _hwnd: int, _dc: object) -> None:
        pass

    def DeleteObject(self, _handle: object) -> None:
        pass

    def DestroyIcon(self, hicon: object) -> None:
        self.destroyed.append(hicon)


def _wait_until_settled(service: IconService, key: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while key in service._pending and time.monotonic() < deadline:
        time.sleep(0.01)


class IconKeyTests(unittest.TestCase):
    def test_folders_share_one_key_regardless_of_name(self) -> None:
        self.assertEqual(icon_key(r"C:\A", "folder"), icon_key(r"D:\Other", "folder"))

    def test_files_share_a_key_by_case_insensitive_extension(self) -> None:
        self.assertEqual(
            icon_key(r"C:\a\report.XLSX", "file"),
            icon_key(r"D:\b\other.xlsx", "file"),
        )

    def test_extensionless_files_do_not_collide_with_folders(self) -> None:
        self.assertNotEqual(icon_key(r"C:\README", "file"), icon_key(r"C:\A", "folder"))


class IconServiceTests(unittest.TestCase):
    def test_unavailable_without_win32_dependencies_never_spawns_work(self) -> None:
        service = IconService(dependencies=None)
        self.assertFalse(service.available)

        calls: list[tuple[str, object]] = []
        service.request("file", r"C:\a.txt", lambda key, image: calls.append((key, image)))

        self.assertEqual([], calls)
        self.assertIsNone(service.get_cached("file"))

    def test_request_extracts_off_thread_and_delivers_to_callback(self) -> None:
        win32gui = _FakeWin32Gui()
        service = IconService(dependencies=(win32gui, _FakeWin32Ui()), size=16)
        done = threading.Event()
        received: list[tuple[str, IconImage]] = []

        def callback(key: str, image: IconImage) -> None:
            received.append((key, image))
            done.set()

        service.request("file", r"C:\a.txt", callback)

        self.assertTrue(done.wait(2.0))
        self.assertEqual(1, len(received))
        key, image = received[0]
        self.assertEqual("file", key)
        self.assertEqual((16, 16), (image.width, image.height))
        self.assertEqual(16 * 16 * 4, len(image.bgra))
        self.assertEqual(image, service.get_cached("file"))
        self.assertEqual([win32gui.hicon], win32gui.destroyed)

    def test_concurrent_requests_for_the_same_key_extract_only_once(self) -> None:
        win32gui = _FakeWin32Gui()
        service = IconService(dependencies=(win32gui, _FakeWin32Ui()))

        service.request("file", r"C:\a.txt")
        service.request("file", r"C:\b.txt")
        _wait_until_settled(service, "file")

        self.assertEqual(1, len(win32gui.sh_get_file_info_calls))

    def test_extraction_failure_is_cached_as_none_without_a_callback(self) -> None:
        win32gui = _FakeWin32Gui(raises=RuntimeError("shell32 unavailable"))
        service = IconService(dependencies=(win32gui, _FakeWin32Ui()))
        calls: list[object] = []

        service.request("file", r"C:\a.txt", lambda *args: calls.append(args))
        _wait_until_settled(service, "file")

        self.assertIsNone(service.get_cached("file"))
        self.assertEqual([], calls)

    def test_missing_path_is_ignored(self) -> None:
        win32gui = _FakeWin32Gui()
        service = IconService(dependencies=(win32gui, _FakeWin32Ui()))
        service.request("file", "")
        self.assertEqual([], win32gui.sh_get_file_info_calls)


if __name__ == "__main__":
    unittest.main()
