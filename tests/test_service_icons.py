from __future__ import annotations

import threading
import time
import unittest

from quickaccess.services.icons import IconImage, IconService, icon_key


class _FakeIconApi:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    def extract(self, path: str, size: int) -> IconImage | None:
        self.calls.append((path, size))
        if self.raises is not None:
            raise self.raises
        return IconImage(size, size, b"\x11\x22\x33\xff" * size * size)


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

    def test_web_links_share_a_non_file_icon_key(self) -> None:
        self.assertEqual("\0url", icon_key("https://example.com", "url"))
        self.assertNotEqual(icon_key("https://example.com", "url"), icon_key("index", "file"))


class IconServiceTests(unittest.TestCase):
    def test_unavailable_without_native_api_never_spawns_work(self) -> None:
        service = IconService(api=None)
        self.assertFalse(service.available)
        calls: list[tuple[str, object]] = []
        service.request("file", r"C:\a.txt", lambda key, image: calls.append((key, image)))
        self.assertEqual([], calls)
        self.assertIsNone(service.get_cached("file"))

    def test_request_extracts_off_thread_and_delivers_to_callback(self) -> None:
        api = _FakeIconApi()
        service = IconService(api=api, size=16)
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
        self.assertEqual([(r"C:\a.txt", 16)], api.calls)

    def test_concurrent_requests_for_same_key_extract_only_once(self) -> None:
        api = _FakeIconApi()
        service = IconService(api=api)
        service.request("file", r"C:\a.txt")
        service.request("file", r"C:\b.txt")
        _wait_until_settled(service, "file")
        self.assertEqual(1, len(api.calls))

    def test_extraction_failure_is_cached_without_callback(self) -> None:
        api = _FakeIconApi(raises=RuntimeError("shell32 unavailable"))
        service = IconService(api=api)
        calls: list[object] = []
        service.request("file", r"C:\a.txt", lambda *args: calls.append(args))
        _wait_until_settled(service, "file")
        self.assertIsNone(service.get_cached("file"))
        self.assertEqual([], calls)

    def test_missing_path_is_ignored(self) -> None:
        api = _FakeIconApi()
        service = IconService(api=api)
        service.request("file", "")
        self.assertEqual([], api.calls)


if __name__ == "__main__":
    unittest.main()
