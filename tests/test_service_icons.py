from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable

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


class _FlakyIconApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def extract(self, path: str, size: int) -> IconImage | None:
        self.calls.append((path, size))
        if len(self.calls) == 1:
            return None
        return IconImage(size, size, b"\x11\x22\x33\xff" * size * size)


def _wait_until_settled(service: IconService, key: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service._lock:
            if key not in service._pending:
                return
        time.sleep(0.01)


def _wait_until(predicate: Callable[[], object], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class IconKeyTests(unittest.TestCase):
    def test_folders_share_one_key_regardless_of_name(self) -> None:
        self.assertEqual(icon_key(r"C:\A", "folder"), icon_key(r"D:\Other", "folder"))

    def test_files_share_a_key_by_case_insensitive_extension(self) -> None:
        self.assertEqual(
            icon_key(r"C:\a\report.XLSX", "file"),
            icon_key(r"D:\b\other.xlsx", "file"),
        )

    def test_executables_shortcuts_and_icon_files_use_path_specific_keys(self) -> None:
        for extension in ("exe", "lnk", "ico"):
            with self.subTest(extension=extension):
                first = icon_key(rf"C:\Apps\First.{extension}", "file")
                same = icon_key(rf"c:\apps\.\FIRST.{extension.upper()}", "file")
                second = icon_key(rf"C:\Apps\Second.{extension}", "file")
                self.assertEqual(first, same)
                self.assertNotEqual(first, second)

    def test_extensionless_files_do_not_collide_with_folders(self) -> None:
        self.assertNotEqual(icon_key(r"C:\README", "file"), icon_key(r"C:\A", "folder"))

    def test_web_links_share_a_non_file_icon_key(self) -> None:
        self.assertEqual("\0url", icon_key("https://example.com", "url"))
        self.assertNotEqual(icon_key("https://example.com", "url"), icon_key("index", "file"))


class IconServiceTests(unittest.TestCase):
    def make_service(self, **kwargs: object) -> IconService:
        service = IconService(**kwargs)
        self.addCleanup(service.close)
        return service

    def test_unavailable_without_native_api_never_spawns_work(self) -> None:
        service = self.make_service(api=None)
        self.assertFalse(service.available)
        calls: list[tuple[str, object]] = []
        service.request("file", r"C:\a.txt", lambda key, image: calls.append((key, image)))
        self.assertEqual([], calls)
        self.assertIsNone(service.get_cached("file"))

    def test_request_extracts_off_thread_and_delivers_to_callback(self) -> None:
        api = _FakeIconApi()
        service = self.make_service(api=api, size=16)
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

    def test_success_cache_survives_repeated_panel_requests(self) -> None:
        api = _FakeIconApi()
        service = self.make_service(api=api)
        first_ready = threading.Event()
        unexpected_callback = threading.Event()

        service.request(".txt", r"C:\a.txt", lambda *_args: first_ready.set())
        self.assertTrue(first_ready.wait(2.0))
        cached = service.get_cached(".txt")

        # Opening the panel again asks for every item.  A successful shared
        # key remains hot and must not fall back to another shell lookup.
        service.request(
            ".txt",
            r"D:\another.txt",
            lambda *_args: unexpected_callback.set(),
        )

        self.assertIs(cached, service.get_cached(".txt"))
        self.assertEqual([(r"C:\a.txt", 32)], api.calls)
        self.assertFalse(unexpected_callback.is_set())

    def test_concurrent_requests_for_same_key_extract_only_once(self) -> None:
        api = _FakeIconApi()
        service = self.make_service(api=api)
        service.request("file", r"C:\a.txt")
        service.request("file", r"C:\b.txt")
        _wait_until_settled(service, "file")
        self.assertEqual(1, len(api.calls))

    def test_failed_first_candidate_does_not_starve_valid_same_key_path(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()

        class CandidateApi:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            def extract(self, path: str, size: int) -> IconImage | None:
                self.calls.append((path, size))
                if path == r"C:\BrokenFolder":
                    first_started.set()
                    release_first.wait(2.0)
                    return None
                return IconImage(size, size, b"\x11\x22\x33\xff" * size * size)

        api = CandidateApi()
        service = self.make_service(api=api, max_workers=1, retry_delays=(10.0,))
        done = threading.Event()
        received: list[tuple[str, IconImage]] = []

        def callback(key: str, image: IconImage) -> None:
            received.append((key, image))
            done.set()

        service.request("\0folder", r"C:\BrokenFolder", callback)
        self.assertTrue(first_started.wait(1.0))
        service.request("\0folder", r"D:\ValidFolder", callback)
        release_first.set()

        self.assertTrue(done.wait(2.0))
        self.assertEqual(
            [r"C:\BrokenFolder", r"D:\ValidFolder"],
            [path for path, _size in api.calls],
        )
        self.assertEqual(1, len(received))
        self.assertIsNotNone(service.get_cached("\0folder"))

    def test_later_panel_request_recovers_shared_key_after_failure(self) -> None:
        class CandidateApi:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def extract(self, path: str, size: int) -> IconImage | None:
                self.calls.append(path)
                if path == r"C:\BrokenFolder":
                    return None
                return IconImage(size, size, b"\x11\x22\x33\xff" * size * size)

        api = CandidateApi()
        service = self.make_service(api=api, retry_delays=())
        service.request("\0folder", r"C:\BrokenFolder")
        _wait_until_settled(service, "\0folder")
        self.assertIsNone(service.get_cached("\0folder"))

        done = threading.Event()
        service.request("\0folder", r"D:\ValidFolder", lambda *_args: done.set())

        self.assertTrue(done.wait(2.0))
        self.assertEqual([r"C:\BrokenFolder", r"D:\ValidFolder"], api.calls)
        self.assertIsNotNone(service.get_cached("\0folder"))

    def test_extraction_failure_is_retryable_without_callback(self) -> None:
        api = _FakeIconApi(raises=RuntimeError("shell32 unavailable"))
        service = self.make_service(api=api, retry_delays=())
        calls: list[object] = []
        service.request("file", r"C:\a.txt", lambda *args: calls.append(args))
        _wait_until_settled(service, "file")
        self.assertIsNone(service.get_cached("file"))
        self.assertEqual([], calls)
        service.request("file", r"C:\a.txt")
        _wait_until_settled(service, "file")
        self.assertEqual(2, len(api.calls))

    def test_transient_extraction_failure_retries_in_background(self) -> None:
        api = _FlakyIconApi()
        service = self.make_service(api=api, retry_delays=(0.0,))
        done = threading.Event()
        received: list[tuple[str, IconImage]] = []

        def callback(key: str, image: IconImage) -> None:
            received.append((key, image))
            done.set()

        service.request("file", r"C:\a.txt", callback)
        self.assertTrue(done.wait(2.0))
        self.assertEqual(2, len(api.calls))
        self.assertEqual(1, len(received))
        self.assertEqual(received[0][1], service.get_cached("file"))

    def test_automatic_retries_are_bounded_and_failures_are_not_cached(self) -> None:
        api = _FakeIconApi(raises=RuntimeError("still unavailable"))
        service = self.make_service(api=api, retry_delays=(0.0, 0.0))

        service.request("file", r"C:\a.txt")
        _wait_until_settled(service, "file")

        self.assertEqual(3, len(api.calls))
        self.assertIsNone(service.get_cached("file"))

    def test_close_cancels_pending_retry_timer(self) -> None:
        api = _FakeIconApi(raises=RuntimeError("shell unavailable"))
        service = self.make_service(api=api, retry_delays=(10.0,))
        service.request("file", r"C:\a.txt")

        def retry_is_scheduled() -> bool:
            with service._lock:
                state = service._states.get("file")
                return bool(state and state.retry_timers)

        self.assertTrue(_wait_until(retry_is_scheduled))
        with service._lock:
            timers = tuple(service._states["file"].retry_timers)

        self.assertTrue(service.close())
        for timer in timers:
            timer.join(1.0)
            self.assertFalse(timer.is_alive())
        self.assertEqual(1, len(api.calls))

    def test_close_prevents_callback_from_inflight_extraction(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingSuccessApi:
            def __init__(self) -> None:
                self.calls = 0

            def extract(self, path: str, size: int) -> IconImage:
                self.calls += 1
                started.set()
                release.wait(2.0)
                return IconImage(size, size, b"\x11\x22\x33\xff" * size * size)

        api = BlockingSuccessApi()
        service = self.make_service(api=api, max_workers=1)
        callback_called = threading.Event()
        service.request("file", r"C:\a.txt", lambda *_args: callback_called.set())
        self.assertTrue(started.wait(1.0))

        close_result: list[bool] = []
        closer = threading.Thread(
            target=lambda: close_result.append(service.close(timeout=2.0))
        )
        closer.start()
        self.assertTrue(_wait_until(lambda: not service.available))
        release.set()
        closer.join(2.0)

        self.assertFalse(closer.is_alive())
        self.assertEqual([True], close_result)
        self.assertFalse(callback_called.is_set())
        self.assertTrue(all(not worker.is_alive() for worker in service._workers))
        self.assertTrue(service.close())

    def test_missing_path_is_ignored(self) -> None:
        api = _FakeIconApi()
        service = self.make_service(api=api)
        service.request("file", "")
        self.assertEqual([], api.calls)

    def test_requests_use_a_bounded_worker_pool(self) -> None:
        release = threading.Event()
        started_lock = threading.Lock()
        started = 0
        active = 0
        peak_active = 0

        class BlockingApi:
            def extract(self, path: str, size: int) -> IconImage | None:
                nonlocal active, peak_active, started
                with started_lock:
                    started += 1
                    active += 1
                    peak_active = max(peak_active, active)
                release.wait(2.0)
                with started_lock:
                    active -= 1
                return None

        service = self.make_service(
            api=BlockingApi(), max_workers=2, retry_delays=()
        )
        try:
            for index in range(20):
                service.request(str(index), rf"C:\Apps\{index}.exe")

            def both_workers_started() -> bool:
                with started_lock:
                    return started == 2

            self.assertTrue(_wait_until(both_workers_started))
            with started_lock:
                self.assertEqual(2, started)
                self.assertEqual(2, peak_active)

            close_result: list[bool] = []
            closer = threading.Thread(
                target=lambda: close_result.append(service.close(timeout=2.0))
            )
            closer.start()
            self.assertTrue(_wait_until(lambda: not service.available))
            release.set()
            closer.join(2.0)

            self.assertEqual([True], close_result)
            with started_lock:
                self.assertEqual(2, peak_active)
            self.assertTrue(all(not worker.is_alive() for worker in service._workers))
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
