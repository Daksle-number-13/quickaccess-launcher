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
    CURRENT_SCHEMA_VERSION,
    DEFAULT_APPEARANCE_MODE,
    DEFAULT_HOTKEY,
    DEFAULT_QUICK_ADD_HOTKEY,
    MAX_LAUNCHER_ITEMS,
    LauncherConfig,
    LauncherItemLimitError,
    UnsupportedSchemaVersionError,
    normalize_web_url,
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
        self.assertFalse(config.check_updates)
        self.assertEqual(CURRENT_SCHEMA_VERSION, config.to_dict()["schema_version"])
        self.assertEqual(3, config.columns)
        self.assertEqual(
            [str(home / "Downloads"), str(home / "Documents")],
            [item.path for item in config.items],
        )
        self.assertEqual([0, 1], [item.order for item in config.items])
        self.assertEqual(2, len({item.id for item in config.items}))
        self.assertTrue(all(item.type == "folder" for item in config.items))

    def test_default_uses_redirected_windows_known_folders(self) -> None:
        redirected = [Path(r"D:\받은 파일"), Path(r"E:\OneDrive\문서")]
        with patch("quickaccess.models._known_folder_path", side_effect=redirected):
            config = LauncherConfig.default()

        self.assertEqual([str(path) for path in redirected], [item.path for item in config.items])

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

    def test_legacy_missing_type_never_probes_filesystem_during_load(self) -> None:
        with patch(
            "quickaccess.models.os.path.isdir",
            side_effect=AssertionError("configuration parsing must not touch the network"),
        ):
            config = LauncherConfig.from_data(
                {
                    "items": [
                        {"path": r"\\offline-server\share\folder"},
                        {"path": "https://example.com/docs"},
                    ]
                }
            )

        self.assertEqual(["file", "url"], [item.type for item in config.items])

    def test_safe_field_coercion_and_column_bounds(self) -> None:
        config = LauncherConfig.from_data(
            {
                "hotkey": "",
                "quick_add_hotkey": None,
                "appearance_mode": "LIGHT",
                "run_on_startup": "false",
                "welcome_shown": "yes",
                "check_updates": "true",
                "columns": 99,
                "items": [],
            }
        )

        self.assertEqual(DEFAULT_HOTKEY, config.hotkey)
        self.assertEqual(DEFAULT_QUICK_ADD_HOTKEY, config.quick_add_hotkey)
        self.assertEqual("light", config.appearance_mode)
        self.assertFalse(config.run_on_startup)
        self.assertTrue(config.welcome_shown)
        self.assertTrue(config.check_updates)
        self.assertEqual(5, config.columns)
        self.assertEqual([], config.items)
        self.assertEqual(2, config.set_columns(-5))
        self.assertEqual(3, config.set_columns("not-a-number"))
        self.assertEqual("dark", config.set_appearance_mode("DARK"))
        self.assertEqual("system", config.set_appearance_mode("invalid"))

    def test_unversioned_and_v1_objects_migrate_without_losing_current_fields(self) -> None:
        current_fields = {
            "hotkey": "ctrl+alt+space",
            "quick_add_hotkey": "ctrl+alt+shift+space",
            "appearance_mode": "dark",
            "run_on_startup": False,
            "columns": 4,
            "welcome_shown": True,
            "check_updates": True,
            "last_update_notice": "v9.8.7",
            "items": [
                {
                    "id": "stable-id",
                    "name": "업무 포털",
                    "path": "https://example.com/work?q=1",
                    "type": "url",
                    "order": 0,
                }
            ],
        }

        for schema_version in (None, 1):
            with self.subTest(schema_version=schema_version):
                source = dict(current_fields)
                if schema_version is not None:
                    source["schema_version"] = schema_version
                migrated = LauncherConfig.from_data(source).to_dict()

                self.assertEqual(CURRENT_SCHEMA_VERSION, migrated["schema_version"])
                for field, expected in current_fields.items():
                    self.assertEqual(expected, migrated[field])

    def test_future_schema_has_a_dedicated_error(self) -> None:
        with self.assertRaises(UnsupportedSchemaVersionError) as raised:
            LauncherConfig.from_data(
                {"schema_version": CURRENT_SCHEMA_VERSION + 1, "items": []}
            )

        self.assertEqual(CURRENT_SCHEMA_VERSION + 1, raised.exception.schema_version)
        self.assertEqual(CURRENT_SCHEMA_VERSION, raised.exception.current_version)

    def test_oversized_configuration_is_rejected_before_ui_materialization(self) -> None:
        items = [
            {"name": f"Item {index}", "path": rf"C:\Items\{index}.txt"}
            for index in range(MAX_LAUNCHER_ITEMS + 1)
        ]

        with self.assertRaises(LauncherItemLimitError) as raised:
            LauncherConfig.from_data({"items": items})

        self.assertEqual(MAX_LAUNCHER_ITEMS + 1, raised.exception.count)
        self.assertEqual(MAX_LAUNCHER_ITEMS, raised.exception.limit)

    def test_add_item_limit_fails_without_mutating_existing_items(self) -> None:
        config = LauncherConfig.from_data(
            {
                "items": [
                    {"name": f"Item {index}", "path": rf"C:\Items\{index}.txt"}
                    for index in range(MAX_LAUNCHER_ITEMS)
                ]
            }
        )
        existing_ids = [item.id for item in config.items]

        with self.assertRaises(LauncherItemLimitError):
            config.add_item(r"C:\Items\overflow.txt", name="Overflow")

        self.assertEqual(existing_ids, [item.id for item in config.items])

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

    def test_web_links_are_normalized_and_round_trip(self) -> None:
        config = LauncherConfig(items=[])
        item = config.add_item("example.com/docs?q=1", name="문서", item_type="url")

        self.assertEqual("url", item.type)
        self.assertEqual("https://example.com/docs?q=1", item.path)
        restored = LauncherConfig.from_dict(config.to_dict())
        self.assertEqual("url", restored.items[0].type)
        self.assertEqual(item.path, restored.items[0].path)

    def test_web_link_rejects_unsafe_or_invalid_schemes(self) -> None:
        for value in (
            "",
            "javascript:alert(1)",
            "file:///C:/secret",
            "https://bad host",
            r"https://example.com\@evil.test",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_web_url(value)


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
            self.assertEqual(CURRENT_SCHEMA_VERSION, payload["schema_version"])
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

    def test_atomic_save_retries_transient_windows_sharing_errors(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            store = ConfigStore(path)
            config = LauncherConfig(items=[])
            real_replace = os.replace
            attempts = 0

            def temporarily_locked(source: object, destination: object) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError("temporarily locked")
                    error.winerror = 32  # type: ignore[attr-defined]
                    raise error
                real_replace(source, destination)

            with (
                patch("quickaccess.storage.os.replace", side_effect=temporarily_locked),
                patch("quickaccess.storage.time.sleep") as sleep,
            ):
                store.save(config)

            self.assertEqual(3, attempts)
            self.assertEqual(2, sleep.call_count)
            self.assertTrue(path.is_file())

    def test_atomic_save_stops_after_bounded_replace_retries(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            store = ConfigStore(path)
            error = PermissionError("access denied")
            error.winerror = 5  # type: ignore[attr-defined]

            with (
                patch("quickaccess.storage.os.replace", side_effect=error) as replace,
                patch("quickaccess.storage.time.sleep") as sleep,
                self.assertRaises(PermissionError),
            ):
                store.save(LauncherConfig(items=[]))

            self.assertEqual(4, replace.call_count)
            self.assertEqual(3, sleep.call_count)
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_save_keeps_a_single_rolling_backup_of_the_previous_version(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            store = ConfigStore(path)
            backup_path = path.with_name("items.bak.json")

            first = LauncherConfig(items=[])
            first.add_item(r"C:\First", name="첫 번째")
            store.save(first)
            self.assertFalse(backup_path.exists())

            second = LauncherConfig(items=[])
            second.add_item(r"C:\Second", name="두 번째")
            store.save(second)

            self.assertTrue(backup_path.exists())
            backed_up = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual("첫 번째", backed_up["items"][0]["name"])
            current = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("두 번째", current["items"][0]["name"])

            third = LauncherConfig(items=[])
            third.add_item(r"C:\Third", name="세 번째")
            store.save(third)
            backed_up = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual("두 번째", backed_up["items"][0]["name"])

    def test_legacy_schema_is_migrated_and_rewritten(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            legacy = [
                {
                    "id": "legacy-stable-id",
                    "name": "업무",
                    "path": r"C:\Work",
                    "type": "folder",
                    "order": 7,
                }
            ]
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

            result = ConfigStore(path).load()

            self.assertTrue(result.migrated)
            self.assertFalse(result.recovered)
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rewritten, dict)
            self.assertEqual(CURRENT_SCHEMA_VERSION, rewritten["schema_version"])
            self.assertEqual("system", rewritten["appearance_mode"])
            self.assertEqual(0, rewritten["items"][0]["order"])
            self.assertEqual("legacy-stable-id", rewritten["items"][0]["id"])
            self.assertEqual("업무", rewritten["items"][0]["name"])
            self.assertEqual(r"C:\Work", rewritten["items"][0]["path"])
            self.assertEqual("folder", rewritten["items"][0]["type"])

    def test_unversioned_and_v1_object_files_are_rewritten_as_v2(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            for schema_version in (None, 1):
                with self.subTest(schema_version=schema_version):
                    source = {
                        "hotkey": "ctrl+alt+space",
                        "quick_add_hotkey": "ctrl+alt+shift+space",
                        "appearance_mode": "dark",
                        "run_on_startup": False,
                        "columns": 4,
                        "welcome_shown": True,
                        "check_updates": True,
                        "last_update_notice": "v8.7.6",
                        "items": [
                            {
                                "id": "preserved-id",
                                "name": "보존 항목",
                                "path": r"C:\Work\preserved.txt",
                                "type": "file",
                                "order": 0,
                            }
                        ],
                    }
                    if schema_version is not None:
                        source["schema_version"] = schema_version
                    path.write_text(
                        json.dumps(source, ensure_ascii=False), encoding="utf-8"
                    )

                    result = ConfigStore(path).load()

                    self.assertTrue(result.migrated)
                    rewritten = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(CURRENT_SCHEMA_VERSION, rewritten["schema_version"])
                    for field in source:
                        if field != "schema_version":
                            self.assertEqual(source[field], rewritten[field])

    def test_future_schema_file_is_rejected_without_touching_any_bytes(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            original = (
                b'{\r\n  "schema_version": 999,\r\n'
                b'  "future_field": {"must": "survive"},\r\n'
                b'  "items": []\r\n}\r\n'
            )
            path.write_bytes(original)

            with self.assertRaises(UnsupportedSchemaVersionError):
                ConfigStore(path).load()

            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], list(path.parent.glob("items.corrupt-*.json")))
            self.assertFalse(path.with_name("items.bak.json").exists())

    def test_corrupt_json_is_backed_up_and_defaults_are_recovered(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            corrupt_bytes = b'{"hotkey": "ctrl+space", broken'
            path.write_bytes(corrupt_bytes)

            result = ConfigStore(path, user_home=temporary_directory).load()

            self.assertTrue(result.recovered)
            self.assertFalse(result.restored_from_backup)
            self.assertFalse(result.created)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertTrue(result.backup_path.exists())
            self.assertEqual(corrupt_bytes, result.backup_path.read_bytes())
            self.assertEqual(2, len(result.config.items))
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_corrupt_json_restores_valid_rolling_backup_before_defaults(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            backup_path = path.with_name("items.bak.json")
            corrupt_bytes = b'{"hotkey": broken'
            path.write_bytes(corrupt_bytes)
            expected = LauncherConfig(items=[])
            expected.run_on_startup = False
            expected.add_item(r"C:\Recovered", name="백업 항목", item_type="folder")
            backup_path.write_text(
                json.dumps(expected.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            result = ConfigStore(path).load()

            self.assertTrue(result.recovered)
            self.assertTrue(result.restored_from_backup)
            self.assertEqual("백업 항목", result.config.items[0].name)
            self.assertFalse(result.config.run_on_startup)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(corrupt_bytes, result.backup_path.read_bytes())
            restored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("백업 항목", restored["items"][0]["name"])

    def test_invalid_rolling_backup_falls_back_to_defaults(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            path.write_text("not-json", encoding="utf-8")
            path.with_name("items.bak.json").write_text(
                '{"items": "also-invalid"}',
                encoding="utf-8",
            )

            result = ConfigStore(path, user_home=temporary_directory).load()

            self.assertTrue(result.recovered)
            self.assertFalse(result.restored_from_backup)
            self.assertEqual(2, len(result.config.items))

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

    def test_corrupt_backup_retention_is_bounded_and_keeps_rolling_backup(self) -> None:
        with writable_test_directory() as temporary_directory:
            path = Path(temporary_directory) / "items.json"
            rolling_backup = path.with_name("items.bak.json")
            rolling_contents = LauncherConfig(items=[]).to_dict()
            rolling_backup.write_text(
                json.dumps(rolling_contents, ensure_ascii=False), encoding="utf-8"
            )
            store = ConfigStore(path, max_corrupt_backups=3)

            latest_backup: Path | None = None
            for index in range(7):
                path.write_bytes(f"broken-{index}".encode())
                result = store.load()
                latest_backup = result.backup_path

            corrupt_backups = list(path.parent.glob("items.corrupt-*.json"))
            self.assertEqual(3, len(corrupt_backups))
            self.assertIsNotNone(latest_backup)
            assert latest_backup is not None
            self.assertIn(latest_backup, corrupt_backups)
            self.assertTrue(rolling_backup.exists())
            self.assertEqual(
                rolling_contents,
                json.loads(rolling_backup.read_text(encoding="utf-8")),
            )

    def test_corrupt_backup_retention_rejects_invalid_limits(self) -> None:
        with self.assertRaises(TypeError):
            ConfigStore(max_corrupt_backups=True)
        with self.assertRaises(ValueError):
            ConfigStore(max_corrupt_backups=0)


if __name__ == "__main__":
    unittest.main()
