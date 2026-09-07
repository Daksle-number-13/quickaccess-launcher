from __future__ import annotations

import threading
import time
import unittest

from quickaccess.services.launcher import FileLauncher, LaunchErrorCode
from quickaccess.services.validation import PathStatus, PathValidationService


class PathValidationTests(unittest.TestCase):
    def test_worker_count_requires_a_positive_integer(self) -> None:
        for invalid in (True, False, 0, -1, 0.5, "4"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PathValidationService(lambda _result: None, max_workers=invalid)  # type: ignore[arg-type]

    def test_timeout_values_must_be_finite_and_positive(self) -> None:
        invalid_values = (float("nan"), float("inf"), float("-inf"), 0.0, -1.0)
        for value in invalid_values:
            with self.subTest(timeout_seconds=value):
                with self.assertRaises(ValueError):
                    PathValidationService(lambda _result: None, timeout_seconds=value)
            with self.subTest(queue_timeout_seconds=value):
                with self.assertRaises(ValueError):
                    PathValidationService(
                        lambda _result: None,
                        queue_timeout_seconds=value,
                    )

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
        broken = {result.item_id: result.is_broken for result in results}
        self.assertFalse(broken["one"])
        self.assertTrue(broken["two"])
        self.assertTrue(broken["three"])

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
            self.assertFalse(received[0].is_broken)
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

    def test_blocked_calls_and_replacements_remain_strictly_bounded(self) -> None:
        release = threading.Event()
        initial_timeouts = threading.Event()
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
            if len(received) >= 3:
                initial_timeouts.set()

        validator = PathValidationService(
            callback,
            timeout_seconds=0.05,
            exists=blocked_exists,
            max_workers=3,
        )
        try:
            for index in range(20):
                validator.validate(str(index), rf"\\server\offline\{index}")
            self.assertTrue(initial_timeouts.wait(1))
            time.sleep(0.1)
            with started_lock:
                # Three baseline calls plus at most three abandoned-call
                # replacements can be inside an uninterruptible OS call.
                self.assertLessEqual(started, 6)
            self.assertTrue(all(result.status is PathStatus.TIMEOUT for result in received))
        finally:
            release.set()
            validator.close()

    def test_queued_local_path_is_probed_after_blocked_worker_times_out(self) -> None:
        release = threading.Event()
        slow_started = threading.Event()
        fast_probed = threading.Event()
        ready = threading.Event()
        received = []

        def exists(path: str) -> bool:
            if path.startswith(r"\\server"):
                slow_started.set()
                release.wait(2)
                return True
            fast_probed.set()
            return True

        def callback(result: object) -> None:
            received.append(result)
            if len(received) == 2:
                ready.set()

        validator = PathValidationService(
            callback,
            timeout_seconds=0.05,
            exists=exists,
            max_workers=1,
        )
        try:
            validator.validate("slow", r"\\server\offline")
            self.assertTrue(slow_started.wait(1))
            validator.validate("fast", r"C:\ready.txt")

            self.assertTrue(ready.wait(1))
            statuses = {result.item_id: result.status for result in received}
            self.assertEqual(PathStatus.TIMEOUT, statuses["slow"])
            self.assertEqual(PathStatus.VALID, statuses["fast"])
            self.assertTrue(fast_probed.is_set())
        finally:
            release.set()
            validator.close()

    def test_saturated_replacement_cap_returns_queued_unknown_warning(self) -> None:
        release = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        local_ready = threading.Event()
        calls: list[str] = []
        received = []

        def exists(path: str) -> bool:
            calls.append(path)
            if path.endswith("one"):
                first_started.set()
            elif path.endswith("two"):
                second_started.set()
            release.wait(2)
            return True

        def callback(result: object) -> None:
            received.append(result)
            if result.item_id == "local":
                local_ready.set()

        validator = PathValidationService(
            callback,
            timeout_seconds=0.05,
            queue_timeout_seconds=0.15,
            exists=exists,
            max_workers=1,
        )
        try:
            validator.validate("one", r"\\server\one")
            self.assertTrue(first_started.wait(1))
            validator.validate("two", r"\\server\two")
            self.assertTrue(second_started.wait(1))

            # The only replacement is now stuck too.  A later local request
            # must resolve to an unknown warning rather than remain pending.
            validator.validate("local", r"C:\ready.txt")
            self.assertTrue(local_ready.wait(1))
            local = next(result for result in received if result.item_id == "local")
            self.assertEqual(PathStatus.TIMEOUT, local.status)
            self.assertFalse(local.is_broken)
            self.assertIn("could not start", local.error or "")
            self.assertNotIn(r"C:\ready.txt", calls)
            self.assertLessEqual(len(validator._workers), 2)
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
