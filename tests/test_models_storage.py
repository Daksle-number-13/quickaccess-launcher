from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from quickaccess.models import (
    DEFAULT_APPEARANCE_MODE,
    DEFAULT_HOTKEY,
    DEFAULT_QUICK_ADD_HOTKEY,
    LauncherConfig,
)
from quickaccess.storage import ConfigStore


TEST_TEMP_ROOT = Path(__file__).resolve().parent


@contextmanager
def writable_test_directory() -> Iterator[str]:
    """Avoid tempfile's Windows 0o700 ACL, which some sandboxes reject."""

    path = TEST_TEMP_ROOT / f"_case_{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path)


class LauncherConfigTests(unittest.TestCase):
    def test_defaults_include_specified_folders_and_unique_ordered_ids(self) -> None:
        home = Path(r"C:\Users\Tester")
        config = LauncherConfig.default(user_home=home)

        self.assertEqual(DEFAULT_HOTKEY, config.hotkey)
        self.assertEqual(DEFAULT_QUICK_ADD_HOTKEY, config.quick_add_hotkey)
        self.assertEqual(DEFAULT_APPEARANCE_MODE, config.appearance_mode)
        self.assertTrue(config.run_on_startup)
        self.assertFalse(config.welcome_shown)
        self.assertEqual(3, config.columns)
        self.assertEqual(
            [str(home / "Downloads"), str(home / "Documents")],
            [item.path for item in config.items],
        )
        self.assertEqual([0, 1], [item.order for item in config.items])
        self.assertEqual(2, len({item.id for item in config.items}))
        self.assertTrue(all(item.type == "folder" for item in config.items))

    def test_legacy_list_migrates_missing_fields_and_duplicate_ids(self) -> None:
        config = LauncherConfig.from_data(
            [
                {
                    "id": "duplicate",
                    "name": "두 번째 순서",
                    "path": r"C:\Second",
                    "order": 8,
                },
                {
                    "id": "duplicate",
                    "path": r"C:\First\문서.xlsx",
                    "type": "file",
                    "order": 1,
                },
                {"name": "invalid-without-path"},
            ]
        )

        self.assertEqual(2, len(config.items))
        self.assertEqual([0, 1], [item.order for item in config.items])
        self.assertEqual("문서.xlsx", config.items[0].name)
        self.assertEqual("두 번째 순서", config.items[1].name)
        self.assertEqual(2, len({item.id for item in config.items}))

    def test_safe_field_coercion_and_column_bounds(self) -> None:
        config = LauncherConfig.from_data(
            {
                "hotkey": "",
                "quick_add_hotkey": None,
                "appearance_mode": "LIGHT",
                "run_on_startup": "false",
                "welcome_shown": "yes",
                "columns": 99,
                "items": [],
            }
        )

        self.assertEqual(DEFAULT_HOTKEY, config.hotkey)
        self.assertEqual(DEFAULT_QUICK_ADD_HOTKEY, config.quick_add_hotkey)
        self.assertEqual("light", config.appearance_mode)
        self.assertFalse(config.run_on_startup)
        self.assertTrue(config.welcome_shown)
        self.assertEqual(5, config.columns)
        self.assertEqual([], config.items)
        self.assertEqual(2, config.set_columns(-5))
        self.assertEqual(3, config.set_columns("not-a-number"))
        self.assertEqual("dark", config.set_appearance_mode("DARK"))
        self.assertEqual("system", config.set_appearance_mode("invalid"))

    def test_mutations_keep_fixed_contiguous_order(self) -> None:
        with writable_test_directory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "폴더"
            folder.mkdir()
            file_path = root / "문서.txt"
            file_path.write_text("내용", encoding="utf-8")

            config = LauncherConfig(items=[])
            folder_item = config.add_item(folder, name="작업 폴더")
            file_item = config.add(file_path)
            self.assertEqual("folder", folder_item.type)
            self.assertEqual("file", file_item.type)

            config.rename(file_item.id, "품질 문서")
            config.move(file_item.id, 0)
            self.assertEqual([file_item.id, folder_item.id], [i.id for i in config.items])
            self.assertEqual([0, 1], [i.order for i in config.items])

            config.replace_path(file_item.id, folder)
            self.assertEqual("folder", file_item.type)
            self.assertEqual("품질 문서", file_item.name)

            removed = config.delete(folder_item.id)
            self.assertIs(removed, folder_item)
            self.assertEqual([0], [item.order for item in config.items])

            with self.assertRaises(KeyError):
                config.rename_item("missing", "name")
            with self.assertRaises(ValueError):
                config.rename_item(file_item.id, "   ")


class ConfigStoreTests(unittest.TestCase):
    def test_default_path_uses_appdata(self) -> None:
        with writable_test_directory() as temporary_directory:
            with patch.dict(os.environ, {"APPDATA": temporary_directory}):
                self.assertEqual(
                    Path(temporary_directory) / "QuickAccess" / "items.json",
                    ConfigStore.default_path(),
                )

    def test_missing_file_is_created_in_utf8_object_schema(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "QuickAccess" / "items.json"
            store = ConfigStore(path, user_home=Path(temporary_directory) / "사용자")

            result = store.load()

            self.assertTrue(result.created)
            self.assertFalse(result.recovered)
            self.assertTrue(result.changed_on_disk)
            raw_bytes = path.read_bytes()
            self.assertIn("다운로드".encode("utf-8"), raw_bytes)
            self.assertNotIn(b"\\ub2e4\\uc6b4", raw_bytes)
            payload = json.loads(raw_bytes.decode("utf-8"))
            self.assertIsInstance(payload, dict)
            self.assertIn("welcome_shown", payload)
            self.assertEqual("system", payload["appearance_mode"])
            self.assertEqual([0, 1], [item["order"] for item in payload["items"]])

    def test_save_is_atomic_and_round_trips(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            store = ConfigStore(path)
            config = LauncherConfig(items=[])
            config.add_item(r"C:\품질\불량집계.xlsx", name="불량집계", item_type="file")

            real_replace = os.replace
            with patch("quickaccess.storage.os.replace", wraps=real_replace) as replace:
                store.save(config)

            self.assertEqual(1, replace.call_count)
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))
            loaded = store.load()
            self.assertFalse(loaded.changed_on_disk)
            self.assertEqual(config.to_dict(), loaded.config.to_dict())

    def test_legacy_schema_is_migrated_and_rewritten(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            legacy = [{"name": "업무", "path": r"C:\Work", "type": "folder"}]
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

            result = ConfigStore(path).load()

            self.assertTrue(result.migrated)
            self.assertFalse(result.recovered)
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rewritten, dict)
            self.assertEqual("system", rewritten["appearance_mode"])
            self.assertEqual(0, rewritten["items"][0]["order"])
            self.assertTrue(rewritten["items"][0]["id"])

    def test_corrupt_json_is_backed_up_and_defaults_are_recovered(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            corrupt_bytes = b'{"hotkey": "ctrl+space", broken'
            path.write_bytes(corrupt_bytes)

            result = ConfigStore(path, user_home=temporary_directory).load()

            self.assertTrue(result.recovered)
            self.assertFalse(result.created)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertTrue(result.backup_path.exists())
            self.assertEqual(corrupt_bytes, result.backup_path.read_bytes())
            self.assertEqual(2, len(result.config.items))
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_invalid_object_schema_is_recovered_like_corrupt_json(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            path.write_text('{"items": "not-an-array"}', encoding="utf-8")

            result = ConfigStore(path).load()

            self.assertTrue(result.recovered)
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(2, len(result.config.items))

    def test_invalid_item_is_removed_only_after_original_is_backed_up(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            original = {
                "hotkey": "ctrl+space",
                "quick_add_hotkey": "ctrl+shift+space",
                "run_on_startup": False,
                "columns": 3,
                "welcome_shown": True,
                "items": [
                    {"name": "정상", "path": r"C:\정상", "order": 0},
                    {"name": "경로 없음", "order": 1},
                ],
            }
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

            result = ConfigStore(path).load()

            self.assertTrue(result.repaired)
            self.assertTrue(result.migrated)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            backup = json.loads(result.backup_path.read_text(encoding="utf-8"))
            self.assertEqual(2, len(backup["items"]))
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(rewritten["items"]))


if __name__ == "__main__":
    unittest.main()
