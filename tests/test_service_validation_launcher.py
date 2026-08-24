from __future__ import annotations

import threading
import time
import unittest

from quickaccess.services.launcher import FileLauncher, LaunchErrorCode
from quickaccess.services.validation import PathStatus, PathValidationService


class PathValidationTests(unittest.TestCase):
    def test_valid_missing_and_error_results(self) -> None:
        results = []
        ready = threading.Event()

        def exists(path: str) -> bool:
            if path == "error":
                raise OSError("network failure")
            return path == "present"

        def callback(result: object) -> None:
            results.append(result)
            if len(results) == 3:
                ready.set()

        validator = PathValidationService(callback, timeout_seconds=1, exists=exists)
        try:
            validator.validate("one", "present")
            validator.validate("two", "missing")
            validator.validate("three", "error")
            self.assertTrue(ready.wait(1))
        finally:
            validator.close()

        statuses = {result.item_id: result.status for result in results}
        self.assertEqual(statuses["one"], PathStatus.VALID)
        self.assertEqual(statuses["two"], PathStatus.MISSING)
        self.assertEqual(statuses["three"], PathStatus.ERROR)

    def test_timeout_emits_once_and_late_worker_is_ignored(self) -> None:
        release = threading.Event()
        received = []
        ready = threading.Event()

        def blocked_exists(path: str) -> bool:
            release.wait(1)
            return True

        validator = PathValidationService(
            lambda result: (received.append(result), ready.set()),
            timeout_seconds=0.05,
            exists=blocked_exists,
        )
        try:
            validator.validate("slow", r"\\server\offline")
            self.assertTrue(ready.wait(1))
            self.assertEqual(received[0].status, PathStatus.TIMEOUT)
            release.set()
            time.sleep(0.08)
            self.assertEqual(len(received), 1)
        finally:
            release.set()
            validator.close()

    def test_new_generation_suppresses_old_path_result(self) -> None:
        release_old = threading.Event()
        received = []
        ready = threading.Event()

        def exists(path: str) -> bool:
            if path == "old":
                release_old.wait(1)
                return True
            return False

        def callback(result: object) -> None:
            received.append(result)
            ready.set()

        validator = PathValidationService(callback, timeout_seconds=0.5, exists=exists)
        try:
            old_generation = validator.validate("same-item", "old")
            new_generation = validator.validate("same-item", "new")
            self.assertGreater(new_generation, old_generation)
            self.assertTrue(ready.wait(1))
            release_old.set()
            time.sleep(0.05)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].path, "new")
            self.assertEqual(received[0].generation, new_generation)
        finally:
            release_old.set()
            validator.close()

    def test_blocked_paths_never_exceed_worker_limit(self) -> None:
        release = threading.Event()
        all_timed_out = threading.Event()
        started_lock = threading.Lock()
        started = 0
        received = []

        def blocked_exists(_path: str) -> bool:
            nonlocal started
            with started_lock:
                started += 1
            release.wait(1)
            return True

        def callback(result: object) -> None:
            received.append(result)
            if len(received) == 20:
                all_timed_out.set()

        validator = PathValidationService(
            callback,
            timeout_seconds=0.05,
            exists=blocked_exists,
            max_workers=3,
        )
        try:
            for index in range(20):
                validator.validate(str(index), rf"\\server\offline\{index}")
            self.assertTrue(all_timed_out.wait(1))
            with started_lock:
                self.assertLessEqual(started, 3)
            self.assertTrue(all(result.status is PathStatus.TIMEOUT for result in received))
        finally:
            release.set()
            validator.close()


class FileLauncherTests(unittest.TestCase):
    def test_success_calls_startfile_once(self) -> None:
        opened = []
        result = FileLauncher(opened.append).launch(r"C:\품질\불량.xlsx")
        self.assertTrue(result.success)
        self.assertEqual(opened, [r"C:\품질\불량.xlsx"])

    def test_http_url_is_opened_by_the_windows_shell(self) -> None:
        opened: list[str] = []
        result = FileLauncher(opened.append).launch("https://example.com/docs")

        self.assertTrue(result.success)
        self.assertEqual(["https://example.com/docs"], opened)

    def test_shell_exception_is_returned_not_raised(self) -> None:
        def fail(path: str) -> None:
            raise OSError("association missing")

        result = FileLauncher(fail).launch(r"C:\file.unknown")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, LaunchErrorCode.FAILED)
        self.assertIn("association missing", result.error or "")

    def test_invalid_path_is_rejected(self) -> None:
        result = FileLauncher(lambda path: None).launch("")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, LaunchErrorCode.INVALID_PATH)


if __name__ == "__main__":
    unittest.main()
