from __future__ import annotations

import logging
from pathlib import Path
import shutil
import sys
import threading
import types
import unittest
import uuid

from quickaccess.logging_setup import (
    close_logging,
    configure_logging,
    get_log_directory,
    install_exception_hooks,
)


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(f"quickaccess.tests.{id(self)}")
        self.logger.propagate = False

    def tearDown(self) -> None:
        close_logging(logger=self.logger)

    def _new_writable_directory(self) -> Path:
        # Python's TemporaryDirectory uses mode 0o700.  Some managed Windows
        # sandboxes translate that into an ACL the child interpreter cannot
        # reopen, so use a normal workspace directory for filesystem tests.
        directory = Path.cwd() / f".quickaccess-test-{uuid.uuid4().hex}"
        directory.mkdir()
        return directory

    def test_log_directory_prefers_local_appdata(self) -> None:
        path = get_log_directory(
            environ={"LOCALAPPDATA": r"C:\Users\테스트\AppData\Local"}
        )
        self.assertEqual(path.name, "logs")
        self.assertEqual(path.parent.name, "QuickAccess")
        self.assertIn("테스트", str(path))

    def test_configure_is_idempotent_and_rotates_utf8_log(self) -> None:
        directory = self._new_writable_directory()
        try:
            path = configure_logging(
                log_directory=directory,
                logger=self.logger,
                max_bytes=180,
                backup_count=1,
            )
            second_path = configure_logging(
                log_directory=directory,
                logger=self.logger,
                max_bytes=180,
                backup_count=1,
            )
            self.assertEqual(path, second_path)
            self.assertEqual(len(self.logger.handlers), 1)

            for index in range(20):
                self.logger.info("한글 로그 메시지 %s %s", index, "x" * 40)
            for handler in self.logger.handlers:
                handler.flush()

            self.assertTrue(path.exists())
            self.assertTrue(Path(f"{path}.1").exists())
            combined = path.read_text(encoding="utf-8") + Path(
                f"{path}.1"
            ).read_text(encoding="utf-8")
            self.assertIn("한글", combined)
        finally:
            close_logging(logger=self.logger)
            shutil.rmtree(directory, ignore_errors=True)

    def test_exception_hooks_log_process_and_thread_failures(self) -> None:
        directory = self._new_writable_directory()
        hooks = None
        try:
            path = configure_logging(log_directory=directory, logger=self.logger)
            hooks = install_exception_hooks(logger=self.logger, chain=False)
            try:
                try:
                    raise ValueError("main boom")
                except ValueError:
                    exc_type, exc_value, traceback = sys.exc_info()
                    assert exc_type is not None and exc_value is not None
                    sys.excepthook(exc_type, exc_value, traceback)

                try:
                    raise RuntimeError("worker boom")
                except RuntimeError:
                    exc_type, exc_value, traceback = sys.exc_info()
                    args = types.SimpleNamespace(
                        exc_type=exc_type,
                        exc_value=exc_value,
                        exc_traceback=traceback,
                        thread=threading.current_thread(),
                    )
                    threading.excepthook(args)
            finally:
                hooks.restore()

            for handler in self.logger.handlers:
                handler.flush()
            contents = path.read_text(encoding="utf-8")
            self.assertIn("main boom", contents)
            self.assertIn("worker boom", contents)
            self.assertFalse(hooks.active)
        finally:
            if hooks is not None:
                hooks.restore()
            close_logging(logger=self.logger)
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
