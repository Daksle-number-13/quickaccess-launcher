from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import unittest
from unittest.mock import patch
import uuid

from quickaccess.models import (
    CURRENT_SCHEMA_VERSION,
    LauncherConfig,
    LauncherItem,
    UnsupportedSchemaVersionError,
)
from quickaccess.services.config_transfer import (
    PORTABLE_FORMAT,
    PORTABLE_FORMAT_VERSION,
    PortableConfigError,
    UnsupportedPortableFormatVersionError,
    apply_config_import,
    build_portable_payload,
    export_portable_config,
    preview_config_import,
    write_portable_config,
)


TEST_TEMP_ROOT = Path(__file__).resolve().parent


def _document(
    items: list[object],
    **overrides: object,
) -> dict[str, object]:
    config: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "hotkey": "ctrl+alt+space",
        "quick_add_hotkey": "ctrl+alt+shift+space",
        "appearance_mode": "dark",
        "run_on_startup": False,
        "columns": 4,
        "welcome_shown": True,
        "check_updates": True,
        "last_update_notice": "v2.0.0",
        "items": items,
    }
    config.update(overrides)
    return {
        "format": PORTABLE_FORMAT,
        "format_version": PORTABLE_FORMAT_VERSION,
        "config_schema_version": CURRENT_SCHEMA_VERSION,
        "config": config,
    }


def _item(
    item_id: str,
    name: str,
    path: str,
    item_type: str,
    order: int,
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "path": path,
        "type": item_type,
        "order": order,
    }


def _snapshot(config: LauncherConfig) -> tuple[object, ...]:
    return (
        config.hotkey,
        config.quick_add_hotkey,
        config.appearance_mode,
        config.run_on_startup,
        config.columns,
        tuple(
            (item.id, item.name, item.path, item.type, item.order)
            for item in config.items
        ),
    )


class ConfigTransferTests(unittest.TestCase):
    def test_export_has_explicit_metadata_and_is_deterministic_without_mutation(self) -> None:
        config = LauncherConfig(
            appearance_mode="light",
            items=[
                LauncherItem(
                    id="stable-id",
                    name="업무 문서",
                    path=r"C:\업무\문서.xlsx",
                    type="file",
                    order=0,
                )
            ],
        )
        before = _snapshot(config)

        first = export_portable_config(config)
        second = export_portable_config(config)
        payload = json.loads(first)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertIn("업무 문서", first)
        self.assertEqual(PORTABLE_FORMAT, payload["format"])
        self.assertEqual(PORTABLE_FORMAT_VERSION, payload["format_version"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, payload["config_schema_version"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, payload["config"]["schema_version"])
        self.assertEqual("stable-id", payload["config"]["items"][0]["id"])
        self.assertEqual(before, _snapshot(config))
        self.assertEqual(payload, build_portable_payload(config))

    def test_merge_preview_reports_every_outcome_and_preserves_current_settings(self) -> None:
        current = LauncherConfig(
            hotkey="ctrl+space",
            appearance_mode="light",
            columns=2,
            items=[
                LauncherItem("기존 A", r"C:\Old", "folder", 0, "id-a"),
                LauncherItem("기존 B", r"C:\Keep.txt", "file", 1, "id-b"),
            ],
        )
        before = _snapshot(current)
        source = _document(
            [
                _item("id-a", "수정 A", r"D:\New", "folder", 0),
                _item("id-b", "기존 B", r"C:\Keep.txt", "file", 1),
                _item("id-c", "문서 사이트", "example.com/docs", "url", 2),
                _item(
                    "id-d",
                    "중복 사이트",
                    "https://EXAMPLE.com/docs",
                    "url",
                    3,
                ),
                _item("bad-url", "잘못된 링크", "javascript:alert(1)", "url", 4),
            ]
        )

        preview = preview_config_import(current, json.dumps(source), mode="merge")

        self.assertEqual(
            {
                "added": 1,
                "updated": 1,
                "skipped": 1,
                "invalid": 1,
                "conflicts": 1,
                "removed": 0,
            },
            preview.counts,
        )
        self.assertFalse(preview.settings_changed)
        self.assertTrue(preview.changed)
        self.assertEqual("id-c", preview.added[0].item_id)
        self.assertEqual("id-a", preview.updated[0].item_id)
        self.assertEqual("id-b", preview.skipped[0].item_id)
        self.assertEqual("bad-url", preview.invalid[0].item_id)
        self.assertEqual("id-d", preview.conflicts[0].item_id)
        self.assertEqual(before, _snapshot(current), "preview must not mutate current")

        applied = apply_config_import(preview)
        self.assertEqual(["id-a", "id-b", "id-c"], [item.id for item in applied.items])
        self.assertEqual([0, 1, 2], [item.order for item in applied.items])
        self.assertEqual("수정 A", applied.items[0].name)
        self.assertEqual("https://example.com/docs", applied.items[2].path)
        self.assertEqual("ctrl+space", applied.hotkey)
        self.assertEqual("light", applied.appearance_mode)
        self.assertEqual(2, applied.columns)
        self.assertEqual(before, _snapshot(current), "apply must not mutate current")

        applied.items[0].name = "caller mutation"
        self.assertEqual("수정 A", apply_config_import(preview).items[0].name)

    def test_replace_applies_settings_import_order_and_stable_ids(self) -> None:
        current = LauncherConfig(
            appearance_mode="light",
            columns=2,
            items=[
                LauncherItem("공유", r"C:\Shared.txt", "file", 0, "shared"),
                LauncherItem("삭제 예정", r"C:\Old.txt", "file", 1, "old"),
            ],
        )
        source = _document(
            [
                _item("shared", "공유", r"C:\Shared.txt", "file", 20),
                _item("new", "새 링크", "quickaccess.example/help", "url", 5),
            ]
        )

        preview = preview_config_import(current, source, mode="replace")
        applied = apply_config_import(preview)

        self.assertEqual(1, len(preview.added))
        self.assertEqual("new", preview.added[0].item_id)
        self.assertEqual(1, len(preview.updated))
        self.assertEqual("shared", preview.updated[0].item_id)
        self.assertEqual(("old",), preview.removed_item_ids)
        self.assertTrue(preview.settings_changed)
        self.assertEqual(["new", "shared"], [item.id for item in applied.items])
        self.assertEqual([0, 1], [item.order for item in applied.items])
        self.assertEqual("https://quickaccess.example/help", applied.items[0].path)
        self.assertEqual("dark", applied.appearance_mode)
        self.assertEqual("ctrl+alt+space", applied.hotkey)
        self.assertEqual(4, applied.columns)

    def test_duplicate_ids_and_targets_are_conflicts_not_silent_rewrites(self) -> None:
        current = LauncherConfig(items=[])
        source = _document(
            [
                _item("first", "첫 항목", r"C:\One.txt", "file", 0),
                _item("first", "중복 ID", r"C:\Two.txt", "file", 1),
                _item("third", "중복 대상", r"c:\one.txt", "folder", 2),
            ]
        )

        preview = preview_config_import(current, source, mode="merge")
        applied = apply_config_import(preview)

        self.assertEqual(1, len(preview.added))
        self.assertEqual(2, len(preview.conflicts))
        self.assertIn("Duplicate item id", preview.conflicts[0].reason)
        self.assertIn("Target already exists", preview.conflicts[1].reason)
        self.assertEqual(["first"], [item.id for item in applied.items])

    def test_future_portable_or_config_schema_is_refused(self) -> None:
        current = LauncherConfig(items=[])
        future_format = _document([])
        future_format["format_version"] = PORTABLE_FORMAT_VERSION + 1
        with self.assertRaises(UnsupportedPortableFormatVersionError):
            preview_config_import(current, future_format)

        future_schema = _document([])
        future_schema["config_schema_version"] = CURRENT_SCHEMA_VERSION + 1
        future_schema["config"]["schema_version"] = CURRENT_SCHEMA_VERSION + 1
        with self.assertRaises(UnsupportedSchemaVersionError):
            preview_config_import(current, future_schema)

        mismatched = _document([])
        mismatched["config"]["schema_version"] = 1
        with self.assertRaisesRegex(PortableConfigError, "does not match"):
            preview_config_import(current, mismatched)

    def test_preview_and_apply_never_touch_or_connect_to_target_paths(self) -> None:
        current = LauncherConfig(items=[])
        source = _document(
            [
                _item(
                    "offline",
                    "오프라인 공유",
                    r"\\offline-server\share\folder",
                    "folder",
                    0,
                ),
                _item("site", "사이트", "example.com", "url", 1),
            ]
        )

        with (
            patch(
                "quickaccess.models.os.path.isdir",
                side_effect=AssertionError("must not inspect imported targets"),
            ),
            patch.object(
                Path,
                "stat",
                side_effect=AssertionError("must not stat imported targets"),
            ),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("must not access the network"),
            ),
        ):
            preview = preview_config_import(current, json.dumps(source).encode("utf-8"))
            applied = apply_config_import(preview)

        self.assertEqual(2, len(applied.items))
        self.assertEqual(r"\\offline-server\share\folder", applied.items[0].path)
        self.assertEqual("https://example.com", applied.items[1].path)

    def test_atomic_export_writes_only_the_selected_test_location(self) -> None:
        directory = TEST_TEMP_ROOT / f"_transfer_{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            destination = directory / "내 설정.quickaccess.json"
            config = LauncherConfig(items=[])

            returned = write_portable_config(destination, config)

            self.assertEqual(destination, returned)
            self.assertEqual(PORTABLE_FORMAT, json.loads(destination.read_text("utf-8"))["format"])
            self.assertEqual([], list(directory.glob("*.tmp")))
        finally:
            shutil.rmtree(directory)

    def test_malformed_documents_fail_before_an_apply_plan_exists(self) -> None:
        current = LauncherConfig(items=[])
        invalid_items = _document([])
        invalid_items["config"]["items"] = "not-an-array"
        for source in (
            "not-json",
            "[]",
            json.dumps({"format": "something-else"}),
            invalid_items,
        ):
            with self.subTest(source=source), self.assertRaises(PortableConfigError):
                preview_config_import(current, source)

        with self.assertRaises(ValueError):
            preview_config_import(current, _document([]), mode="append")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            apply_config_import(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
