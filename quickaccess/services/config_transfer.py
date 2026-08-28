"""Portable, side-effect-free configuration transfer for QuickAccess.

The import planner deliberately works on plain JSON data.  It never probes,
opens, launches, or resolves a launcher target, so previews stay responsive
even when a configuration contains disconnected network paths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import ntpath
import os
from pathlib import Path
import tempfile
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from ..models import (
    CURRENT_SCHEMA_VERSION,
    LauncherConfig,
    LauncherItem,
    UnsupportedSchemaVersionError,
)


PORTABLE_FORMAT = "quickaccess-launcher-config"
PORTABLE_FORMAT_VERSION = 1

ImportMode = Literal["merge", "replace"]
ImportStatus = Literal["added", "updated", "skipped", "invalid", "conflict"]

_CONFIG_FIELDS = (
    "hotkey",
    "quick_add_hotkey",
    "appearance_mode",
    "run_on_startup",
    "columns",
    "welcome_shown",
    "check_updates",
    "last_update_notice",
)


class PortableConfigError(ValueError):
    """Raised when an import is not a valid QuickAccess portable document."""


class UnsupportedPortableFormatVersionError(PortableConfigError):
    """Raised when a portable document needs a newer importer."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.current_version = PORTABLE_FORMAT_VERSION
        super().__init__(
            "Portable configuration format version "
            f"{version} is newer than supported version {self.current_version}"
        )


@dataclass(frozen=True, slots=True)
class ImportItemReport:
    """One source item's deterministic preview outcome."""

    source_index: int
    status: ImportStatus
    item_id: str | None = None
    name: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ConfigImportPreview:
    """An immutable import plan that can later produce a detached config."""

    mode: ImportMode
    source_schema_version: int
    source_item_count: int
    added: tuple[ImportItemReport, ...] = ()
    updated: tuple[ImportItemReport, ...] = ()
    skipped: tuple[ImportItemReport, ...] = ()
    invalid: tuple[ImportItemReport, ...] = ()
    conflicts: tuple[ImportItemReport, ...] = ()
    removed_item_ids: tuple[str, ...] = ()
    settings_changed: bool = False
    _result_json: str = field(default="", repr=False, compare=False)

    @property
    def changed(self) -> bool:
        return bool(
            self.added
            or self.updated
            or self.removed_item_ids
            or self.settings_changed
        )

    @property
    def counts(self) -> dict[str, int]:
        """Return UI-friendly category counts without exposing mutable state."""

        return {
            "added": len(self.added),
            "updated": len(self.updated),
            "skipped": len(self.skipped),
            "invalid": len(self.invalid),
            "conflicts": len(self.conflicts),
            "removed": len(self.removed_item_ids),
        }


@dataclass(frozen=True, slots=True)
class _ParsedItem:
    source_index: int
    item: LauncherItem


def _config_payload(config: LauncherConfig) -> dict[str, object]:
    """Snapshot ``config`` without calling its mutating ``normalize`` method."""

    if not isinstance(config, LauncherConfig):
        raise TypeError("Expected LauncherConfig")
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        **{name: getattr(config, name) for name in _CONFIG_FIELDS},
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "path": item.path,
                "type": item.type,
                "order": item.order,
            }
            for item in config.items
        ],
    }


def build_portable_payload(config: LauncherConfig) -> dict[str, object]:
    """Build the documented portable envelope as detached JSON-compatible data."""

    return {
        "format": PORTABLE_FORMAT,
        "format_version": PORTABLE_FORMAT_VERSION,
        "config_schema_version": CURRENT_SCHEMA_VERSION,
        "config": _config_payload(config),
    }


def export_portable_config(config: LauncherConfig) -> str:
    """Serialize a config deterministically as human-readable UTF-8 JSON text."""

    return (
        json.dumps(
            build_portable_payload(config),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def write_portable_config(
    destination: str | os.PathLike[str],
    config: LauncherConfig,
) -> Path:
    """Atomically write an export to an explicitly selected destination."""

    path = Path(destination)
    if not path.name:
        raise ValueError("Export destination must be a file path")
    payload = export_portable_config(config)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _positive_version(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PortableConfigError(f"'{field_name}' must be a positive integer")
    return value


def _decode_document(source: str | bytes | bytearray | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(source, Mapping):
        document = source
    elif isinstance(source, (str, bytes, bytearray)):
        try:
            document = json.loads(source)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise PortableConfigError("Portable configuration is not valid JSON") from error
    else:
        raise TypeError("Import source must be JSON text, bytes, or an object mapping")
    if not isinstance(document, Mapping):
        raise PortableConfigError("Portable configuration root must be a JSON object")
    return document


def _parse_document(
    source: str | bytes | bytearray | Mapping[str, object],
) -> tuple[int, Mapping[str, object], Sequence[object]]:
    document = _decode_document(source)
    if document.get("format") != PORTABLE_FORMAT:
        raise PortableConfigError("Unrecognized portable configuration format")

    format_version = _positive_version(document.get("format_version"), "format_version")
    if format_version > PORTABLE_FORMAT_VERSION:
        raise UnsupportedPortableFormatVersionError(format_version)

    schema_version = _positive_version(
        document.get("config_schema_version"), "config_schema_version"
    )
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(schema_version)

    raw_config = document.get("config")
    if not isinstance(raw_config, Mapping):
        raise PortableConfigError("'config' must be a JSON object")
    embedded_schema = _positive_version(
        raw_config.get("schema_version"), "config.schema_version"
    )
    if embedded_schema > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(embedded_schema)
    if embedded_schema != schema_version:
        raise PortableConfigError("Configuration schema metadata does not match")

    raw_items = raw_config.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        raise PortableConfigError("'config.items' must be a JSON array")
    return schema_version, raw_config, raw_items


def _parse_item(raw_item: object, source_index: int) -> _ParsedItem:
    if not isinstance(raw_item, Mapping):
        raise ValueError("Item must be a JSON object")

    item_id = raw_item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("Item is missing a stable id")
    name = raw_item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Item is missing a name")
    path = raw_item.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Item is missing a path")
    item_type = raw_item.get("type")
    if item_type not in ("folder", "file", "url"):
        raise ValueError("Item type must be folder, file, or url")
    order = raw_item.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ValueError("Item order must be a non-negative integer")

    # Supplying a known item type is important: LauncherItem then validates
    # and normalizes text only; it never calls os.path.isdir or touches a path.
    item = LauncherItem(
        id=item_id,
        name=name,
        path=path,
        type=item_type,
        order=order,
    )
    return _ParsedItem(source_index=source_index, item=item)


def _item_report(
    parsed: _ParsedItem,
    status: ImportStatus,
    reason: str = "",
) -> ImportItemReport:
    return ImportItemReport(
        source_index=parsed.source_index,
        status=status,
        item_id=parsed.item.id,
        name=parsed.item.name,
        reason=reason,
    )


def _invalid_report(index: int, raw_item: object, reason: str) -> ImportItemReport:
    item_id: str | None = None
    name = ""
    if isinstance(raw_item, Mapping):
        raw_id = raw_item.get("id")
        raw_name = raw_item.get("name")
        item_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
        name = raw_name.strip() if isinstance(raw_name, str) else ""
    return ImportItemReport(index, "invalid", item_id, name, reason)


def _target_key(item: LauncherItem) -> tuple[str, str]:
    if item.type == "url":
        parsed = urlsplit(item.path)
        canonical = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.casefold(),
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        return ("url", canonical)
    return ("local", ntpath.normcase(ntpath.normpath(item.path.strip())))


def _same_item_content(left: LauncherItem, right: LauncherItem) -> bool:
    return (
        left.id == right.id
        and left.name == right.name
        and left.type == right.type
        and left.path == right.path
    )


def _settings_payload(config: LauncherConfig) -> dict[str, object]:
    return {name: getattr(config, name) for name in _CONFIG_FIELDS}


def _normalized_config(
    settings: Mapping[str, object],
    items: Sequence[LauncherItem],
) -> LauncherConfig:
    data: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        **settings,
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "path": item.path,
                "type": item.type,
                "order": index,
            }
            for index, item in enumerate(items)
        ],
    }
    # A supplied home prevents default-config construction from asking Windows
    # for known-folder locations.  No imported target is inspected here.
    return LauncherConfig.from_data(data, user_home=".")


def _clone_config(config: LauncherConfig) -> LauncherConfig:
    snapshot = _config_payload(config)
    return LauncherConfig.from_data(snapshot, user_home=".")


def _parse_settings(raw_config: Mapping[str, object]) -> LauncherConfig:
    settings_data = {
        "schema_version": raw_config["schema_version"],
        **{name: raw_config.get(name) for name in _CONFIG_FIELDS},
        "items": [],
    }
    return LauncherConfig.from_data(settings_data, user_home=".")


def _preview_replace(
    current: LauncherConfig,
    incoming_settings: LauncherConfig,
    parsed_items: Sequence[_ParsedItem],
    invalid: list[ImportItemReport],
) -> tuple[LauncherConfig, dict[ImportStatus, list[ImportItemReport]], tuple[str, ...]]:
    reports: dict[ImportStatus, list[ImportItemReport]] = {
        "added": [],
        "updated": [],
        "skipped": [],
        "invalid": invalid,
        "conflict": [],
    }
    current_by_id = {item.id: item for item in current.items}
    current_positions = {item.id: index for index, item in enumerate(current.items)}
    accepted: list[LauncherItem] = []
    seen_ids: set[str] = set()
    seen_targets: dict[tuple[str, str], str] = {}

    for parsed in parsed_items:
        item = parsed.item
        target = _target_key(item)
        if item.id in seen_ids:
            reports["conflict"].append(
                _item_report(parsed, "conflict", "Duplicate item id in import")
            )
            continue
        owner = seen_targets.get(target)
        if owner is not None and owner != item.id:
            reports["conflict"].append(
                _item_report(parsed, "conflict", "Another imported item has this target")
            )
            continue

        seen_ids.add(item.id)
        seen_targets[target] = item.id
        new_position = len(accepted)
        accepted.append(item)
        previous = current_by_id.get(item.id)
        if previous is None:
            reports["added"].append(_item_report(parsed, "added"))
        elif _same_item_content(previous, item) and current_positions[item.id] == new_position:
            reports["skipped"].append(
                _item_report(parsed, "skipped", "Item is already identical")
            )
        else:
            reports["updated"].append(_item_report(parsed, "updated"))

    accepted_ids = {item.id for item in accepted}
    removed = tuple(item.id for item in current.items if item.id not in accepted_ids)
    result = _normalized_config(_settings_payload(incoming_settings), accepted)
    return result, reports, removed


def _target_owners(items: Sequence[LauncherItem]) -> dict[tuple[str, str], set[str]]:
    owners: dict[tuple[str, str], set[str]] = {}
    for item in items:
        owners.setdefault(_target_key(item), set()).add(item.id)
    return owners


def _preview_merge(
    current: LauncherConfig,
    parsed_items: Sequence[_ParsedItem],
    invalid: list[ImportItemReport],
) -> tuple[LauncherConfig, dict[ImportStatus, list[ImportItemReport]], tuple[str, ...]]:
    reports: dict[ImportStatus, list[ImportItemReport]] = {
        "added": [],
        "updated": [],
        "skipped": [],
        "invalid": invalid,
        "conflict": [],
    }
    result_items = list(current.items)
    by_id = {item.id: item for item in result_items}
    owners = _target_owners(result_items)
    seen_import_ids: set[str] = set()

    for parsed in parsed_items:
        item = parsed.item
        if item.id in seen_import_ids:
            reports["conflict"].append(
                _item_report(parsed, "conflict", "Duplicate item id in import")
            )
            continue
        seen_import_ids.add(item.id)

        previous = by_id.get(item.id)
        target = _target_key(item)
        target_owners = owners.get(target, set())
        if previous is not None:
            if target_owners - {item.id}:
                reports["conflict"].append(
                    _item_report(parsed, "conflict", "Target belongs to another item")
                )
                continue
            if _same_item_content(previous, item):
                reports["skipped"].append(
                    _item_report(parsed, "skipped", "Item is already identical")
                )
                continue

            old_target = _target_key(previous)
            old_owners = owners.get(old_target)
            if old_owners is not None:
                old_owners.discard(previous.id)
                if not old_owners:
                    del owners[old_target]
            replacement = LauncherItem(
                id=previous.id,
                name=item.name,
                path=item.path,
                type=item.type,
                order=previous.order,
            )
            position = result_items.index(previous)
            result_items[position] = replacement
            by_id[item.id] = replacement
            owners.setdefault(target, set()).add(item.id)
            reports["updated"].append(_item_report(parsed, "updated"))
            continue

        if target_owners:
            reports["conflict"].append(
                _item_report(parsed, "conflict", "Target already exists with another id")
            )
            continue
        appended = LauncherItem(
            id=item.id,
            name=item.name,
            path=item.path,
            type=item.type,
            order=len(result_items),
        )
        result_items.append(appended)
        by_id[item.id] = appended
        owners.setdefault(target, set()).add(item.id)
        reports["added"].append(_item_report(parsed, "added"))

    result = _normalized_config(_settings_payload(current), result_items)
    return result, reports, ()


def preview_config_import(
    current: LauncherConfig,
    source: str | bytes | bytearray | Mapping[str, object],
    *,
    mode: ImportMode = "merge",
) -> ConfigImportPreview:
    """Plan an import without mutating ``current`` or touching any target path."""

    if not isinstance(current, LauncherConfig):
        raise TypeError("current must be LauncherConfig")
    if mode not in ("merge", "replace"):
        raise ValueError("mode must be 'merge' or 'replace'")

    schema_version, raw_config, raw_items = _parse_document(source)
    incoming_settings = _parse_settings(raw_config)
    current_snapshot = _clone_config(current)

    parsed_items: list[_ParsedItem] = []
    invalid: list[ImportItemReport] = []
    for index, raw_item in enumerate(raw_items):
        try:
            parsed_items.append(_parse_item(raw_item, index))
        except (TypeError, ValueError) as error:
            invalid.append(_invalid_report(index, raw_item, str(error)))
    parsed_items.sort(key=lambda parsed: (parsed.item.order, parsed.source_index))

    if mode == "replace":
        result, reports, removed = _preview_replace(
            current_snapshot, incoming_settings, parsed_items, invalid
        )
        settings_changed = _settings_payload(current_snapshot) != _settings_payload(
            incoming_settings
        )
    else:
        result, reports, removed = _preview_merge(
            current_snapshot, parsed_items, invalid
        )
        settings_changed = False

    result_json = json.dumps(_config_payload(result), ensure_ascii=False, sort_keys=True)
    return ConfigImportPreview(
        mode=mode,
        source_schema_version=schema_version,
        source_item_count=len(raw_items),
        added=tuple(reports["added"]),
        updated=tuple(reports["updated"]),
        skipped=tuple(reports["skipped"]),
        invalid=tuple(reports["invalid"]),
        conflicts=tuple(reports["conflict"]),
        removed_item_ids=removed,
        settings_changed=settings_changed,
        _result_json=result_json,
    )


def apply_config_import(preview: ConfigImportPreview) -> LauncherConfig:
    """Materialize a preview as a new config; the original remains unchanged."""

    if not isinstance(preview, ConfigImportPreview):
        raise TypeError("preview must be ConfigImportPreview")
    if not preview._result_json:
        raise PortableConfigError("Import preview does not contain an apply plan")
    return LauncherConfig.from_data(json.loads(preview._result_json), user_home=".")


__all__ = [
    "PORTABLE_FORMAT",
    "PORTABLE_FORMAT_VERSION",
    "ConfigImportPreview",
    "ImportItemReport",
    "ImportMode",
    "PortableConfigError",
    "UnsupportedPortableFormatVersionError",
    "apply_config_import",
    "build_portable_payload",
    "export_portable_config",
    "preview_config_import",
    "write_portable_config",
]
