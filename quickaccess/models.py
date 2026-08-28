"""Domain models for QuickAccess launcher configuration.

The module deliberately contains no GUI concerns.  It owns schema migration,
model invariants, and the stable ordering semantics used by every UI surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import ntpath
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
import uuid


ItemType = Literal["folder", "file", "url"]
AppearanceMode = Literal["system", "light", "dark"]

CURRENT_SCHEMA_VERSION = 2
DEFAULT_HOTKEY = "ctrl+space"
DEFAULT_QUICK_ADD_HOTKEY = "ctrl+shift+space"
DEFAULT_APPEARANCE_MODE: AppearanceMode = "system"
DEFAULT_COLUMNS = 3
MIN_COLUMNS = 2
MAX_COLUMNS = 5
MAX_LAUNCHER_ITEMS = 200
_FOLDERID_DOWNLOADS = "374DE290-123F-4565-9164-39C4925E467B"
_FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


class UnsupportedSchemaVersionError(ValueError):
    """Raised when configuration was written by a newer QuickAccess version."""

    def __init__(
        self,
        schema_version: int,
        *,
        current_version: int = CURRENT_SCHEMA_VERSION,
    ) -> None:
        self.schema_version = schema_version
        self.current_version = current_version
        super().__init__(
            "Configuration schema version "
            f"{schema_version} is newer than supported version {current_version}"
        )


class LauncherItemLimitError(ValueError):
    """Raised before an oversized config can exhaust the resident UI."""

    def __init__(self, count: int, *, limit: int = MAX_LAUNCHER_ITEMS) -> None:
        self.count = count
        self.limit = limit
        super().__init__(
            f"바로가기는 최대 {limit}개까지 등록할 수 있습니다 (요청: {count}개)"
        )


def _new_id() -> str:
    return str(uuid.uuid4())


def _optional_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, os.PathLike):
        return os.fspath(value).strip()
    return ""


def _display_name(path: str) -> str:
    """Return a useful label for either Windows or POSIX-style paths."""

    trimmed = path.rstrip("\\/")
    return ntpath.basename(trimmed) or trimmed or path


def detect_item_type(path: str | os.PathLike[str]) -> ItemType:
    """Classify a path exactly as the product specification requires."""

    try:
        return "folder" if os.path.isdir(os.fspath(path)) else "file"
    except (OSError, TypeError, ValueError):
        return "file"


def _migrate_item_type(path: str) -> ItemType:
    """Classify legacy data without touching a potentially blocking path."""

    try:
        if urlsplit(path).scheme.lower() in ("http", "https"):
            return "url"
    except ValueError:
        pass
    return "file"


def _known_folder_path(folder_id: str) -> Path | None:
    """Resolve a redirected Windows known folder, falling back safely."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = uuid.UUID(folder_id).bytes_le
        guid = _GUID.from_buffer_copy(raw)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        path_pointer = ctypes.c_wchar_p()
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_pointer)
        )
        if result != 0 or not path_pointer.value:
            return None
        try:
            return Path(path_pointer.value)
        finally:
            ole32.CoTaskMemFree(path_pointer)
    except Exception:
        return None


def normalize_web_url(value: str | os.PathLike[str]) -> str:
    """Return a safe HTTP(S) URL, adding HTTPS when the scheme is omitted."""

    text = _optional_text(value)
    if not text:
        raise ValueError("Web URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in text) or "\\" in text:
        raise ValueError("Web URL must not contain whitespace or control characters")
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlsplit(candidate)
        # Accessing ``port`` also rejects malformed values such as ``:abc``.
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Invalid web URL") from error
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only HTTP and HTTPS web URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("Web URLs containing credentials are not supported")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _coerce_order(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback
    return parsed if parsed >= 0 else fallback


def _coerce_columns(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_COLUMNS
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_COLUMNS
    return max(MIN_COLUMNS, min(MAX_COLUMNS, parsed))


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


def _coerce_hotkey(value: object, default: str) -> str:
    text = _optional_text(value)
    return text.lower() if text else default


def _coerce_appearance_mode(value: object) -> AppearanceMode:
    text = _optional_text(value).lower()
    if text in ("system", "light", "dark"):
        return text  # type: ignore[return-value]
    return DEFAULT_APPEARANCE_MODE


def _read_schema_version(data: Mapping[str, object]) -> int | None:
    """Return a validated object-schema version, or ``None`` for legacy data."""

    if "schema_version" not in data:
        return None
    value = data["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Configuration 'schema_version' must be a positive integer")
    if value > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(value)
    return value


@dataclass(slots=True)
class LauncherItem:
    """A single fixed-position launcher button."""

    name: str
    path: str
    type: ItemType
    order: int
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        self.path = _optional_text(self.path)
        if not self.path:
            raise ValueError("Launcher item path must not be empty")

        self.name = _optional_text(self.name) or _display_name(self.path)
        self.id = _optional_text(self.id) or _new_id()
        if self.type not in ("folder", "file", "url"):
            self.type = _migrate_item_type(self.path)
        if self.type == "url":
            self.path = normalize_web_url(self.path)
        self.order = _coerce_order(self.order, 0)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        fallback_order: int = 0,
    ) -> "LauncherItem":
        if not isinstance(data, Mapping):
            raise ValueError("Launcher item must be a JSON object")

        path = _optional_text(data.get("path"))
        if not path:
            raise ValueError("Launcher item is missing a path")

        item_type = data.get("type")
        if item_type not in ("folder", "file", "url"):
            item_type = _migrate_item_type(path)

        return cls(
            id=_optional_text(data.get("id")) or _new_id(),
            name=_optional_text(data.get("name")) or _display_name(path),
            path=path,
            type=item_type,
            order=_coerce_order(data.get("order"), fallback_order),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "order": self.order,
        }


@dataclass(slots=True)
class LauncherConfig:
    """Complete persisted configuration and its mutation operations."""

    hotkey: str = DEFAULT_HOTKEY
    quick_add_hotkey: str = DEFAULT_QUICK_ADD_HOTKEY
    appearance_mode: AppearanceMode = DEFAULT_APPEARANCE_MODE
    run_on_startup: bool = True
    columns: int = DEFAULT_COLUMNS
    welcome_shown: bool = False
    check_updates: bool = False
    last_update_notice: str = ""
    items: list[LauncherItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.normalize()

    @classmethod
    def default(
        cls,
        *,
        user_home: str | os.PathLike[str] | None = None,
    ) -> "LauncherConfig":
        if user_home is None:
            profile = os.environ.get("USERPROFILE")
            home = Path(profile) if profile else Path.home()
            downloads = _known_folder_path(_FOLDERID_DOWNLOADS) or home / "Downloads"
            documents = _known_folder_path(_FOLDERID_DOCUMENTS) or home / "Documents"
        else:
            home = Path(user_home)
            downloads = home / "Downloads"
            documents = home / "Documents"

        return cls(
            items=[
                LauncherItem(
                    name="다운로드",
                    path=str(downloads),
                    type="folder",
                    order=0,
                ),
                LauncherItem(
                    name="문서",
                    path=str(documents),
                    type="folder",
                    order=1,
                ),
            ]
        )

    @classmethod
    def from_data(
        cls,
        data: object,
        *,
        user_home: str | os.PathLike[str] | None = None,
    ) -> "LauncherConfig":
        """Parse current object schema or migrate the legacy top-level list.

        Missing item fields are repaired.  Entries without a usable path are
        skipped, because retaining them would create buttons that cannot ever
        be repaired or launched.
        """

        defaults = cls.default(user_home=user_home)

        if isinstance(data, Mapping):
            # Unversioned objects are the v1 format shipped before explicit
            # schema numbering.  Explicit v1 objects use the same fields, so
            # both can be parsed without lossy intermediate transformations.
            _read_schema_version(data)
            if "items" in data:
                raw_items = data["items"]
                if not isinstance(raw_items, Sequence) or isinstance(
                    raw_items, (str, bytes, bytearray)
                ):
                    raise ValueError("Configuration 'items' must be a JSON array")
            else:
                raw_items = defaults.items

            hotkey = _coerce_hotkey(data.get("hotkey"), DEFAULT_HOTKEY)
            quick_add_hotkey = _coerce_hotkey(
                data.get("quick_add_hotkey"), DEFAULT_QUICK_ADD_HOTKEY
            )
            appearance_mode = _coerce_appearance_mode(data.get("appearance_mode"))
            run_on_startup = _coerce_bool(data.get("run_on_startup"), True)
            columns = _coerce_columns(data.get("columns"))
            welcome_shown = _coerce_bool(data.get("welcome_shown"), False)
            check_updates = _coerce_bool(data.get("check_updates"), False)
            last_update_notice = _optional_text(data.get("last_update_notice"))
        elif isinstance(data, Sequence) and not isinstance(
            data, (str, bytes, bytearray)
        ):
            # The adjacent prototype stored only the item array.
            raw_items = data
            hotkey = DEFAULT_HOTKEY
            quick_add_hotkey = DEFAULT_QUICK_ADD_HOTKEY
            appearance_mode = DEFAULT_APPEARANCE_MODE
            run_on_startup = True
            columns = DEFAULT_COLUMNS
            welcome_shown = False
            check_updates = False
            last_update_notice = ""
        else:
            raise ValueError("Configuration root must be a JSON object or array")

        if len(raw_items) > MAX_LAUNCHER_ITEMS:
            raise LauncherItemLimitError(len(raw_items))

        parsed_items: list[LauncherItem] = []
        for index, raw_item in enumerate(raw_items):
            if isinstance(raw_item, LauncherItem):
                parsed_items.append(raw_item)
                continue
            if not isinstance(raw_item, Mapping):
                continue
            try:
                parsed_items.append(
                    LauncherItem.from_dict(raw_item, fallback_order=index)
                )
            except ValueError:
                continue

        return cls(
            hotkey=hotkey,
            quick_add_hotkey=quick_add_hotkey,
            appearance_mode=appearance_mode,
            run_on_startup=run_on_startup,
            columns=columns,
            welcome_shown=welcome_shown,
            check_updates=check_updates,
            last_update_notice=last_update_notice,
            items=parsed_items,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        user_home: str | os.PathLike[str] | None = None,
    ) -> "LauncherConfig":
        """Named-schema alias for callers that already know the root is an object."""

        return cls.from_data(data, user_home=user_home)

    def normalize(self) -> None:
        """Restore all config invariants without usage-based reordering."""

        self.hotkey = _coerce_hotkey(self.hotkey, DEFAULT_HOTKEY)
        self.quick_add_hotkey = _coerce_hotkey(
            self.quick_add_hotkey, DEFAULT_QUICK_ADD_HOTKEY
        )
        self.appearance_mode = _coerce_appearance_mode(self.appearance_mode)
        self.run_on_startup = _coerce_bool(self.run_on_startup, True)
        self.columns = _coerce_columns(self.columns)
        self.welcome_shown = _coerce_bool(self.welcome_shown, False)
        self.check_updates = _coerce_bool(self.check_updates, False)
        self.last_update_notice = _optional_text(self.last_update_notice)

        valid_items: list[LauncherItem] = []
        for index, item in enumerate(self.items):
            if isinstance(item, LauncherItem):
                valid_items.append(item)
            elif isinstance(item, Mapping):
                try:
                    valid_items.append(
                        LauncherItem.from_dict(item, fallback_order=index)
                    )
                except ValueError:
                    continue

        indexed_items = list(enumerate(valid_items))
        if len(indexed_items) > MAX_LAUNCHER_ITEMS:
            raise LauncherItemLimitError(len(indexed_items))
        indexed_items.sort(key=lambda pair: (pair[1].order, pair[0]))
        self.items = [item for _, item in indexed_items]

        seen_ids: set[str] = set()
        for index, item in enumerate(self.items):
            while not item.id or item.id in seen_ids:
                item.id = _new_id()
            seen_ids.add(item.id)
            item.order = index

    def to_dict(self) -> dict[str, object]:
        self.normalize()
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "hotkey": self.hotkey,
            "quick_add_hotkey": self.quick_add_hotkey,
            "appearance_mode": self.appearance_mode,
            "run_on_startup": self.run_on_startup,
            "columns": self.columns,
            "welcome_shown": self.welcome_shown,
            "check_updates": self.check_updates,
            "last_update_notice": self.last_update_notice,
            "items": [item.to_dict() for item in self.items],
        }

    def get_item(self, item_id: str) -> LauncherItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(f"Unknown launcher item id: {item_id}")

    def add_item(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        item_type: ItemType | None = None,
        item_id: str | None = None,
        position: int | None = None,
    ) -> LauncherItem:
        if len(self.items) >= MAX_LAUNCHER_ITEMS:
            raise LauncherItemLimitError(len(self.items) + 1)
        path_text = _optional_text(path)
        if not path_text:
            raise ValueError("Launcher item path must not be empty")

        resolved_type = (
            item_type
            if item_type in ("folder", "file", "url")
            else detect_item_type(path_text)
        )
        item = LauncherItem(
            id=_optional_text(item_id) or _new_id(),
            name=_optional_text(name) or _display_name(path_text),
            path=path_text,
            type=resolved_type,
            order=len(self.items),
        )

        if position is None:
            self.items.append(item)
        else:
            bounded_position = max(0, min(len(self.items), int(position)))
            self.items.insert(bounded_position, item)
        for index, current_item in enumerate(self.items):
            current_item.order = index
        self.normalize()
        return item

    def delete_item(self, item_id: str) -> LauncherItem:
        item = self.get_item(item_id)
        self.items.remove(item)
        self.normalize()
        return item

    def rename_item(self, item_id: str, new_name: str) -> LauncherItem:
        name = _optional_text(new_name)
        if not name:
            raise ValueError("Launcher item name must not be empty")
        item = self.get_item(item_id)
        item.name = name
        return item

    def move_item(self, item_id: str, new_index: int) -> LauncherItem:
        item = self.get_item(item_id)
        old_index = self.items.index(item)
        self.items.pop(old_index)
        bounded_index = max(0, min(len(self.items), int(new_index)))
        self.items.insert(bounded_index, item)
        for index, current_item in enumerate(self.items):
            current_item.order = index
        self.normalize()
        return item

    def replace_path(
        self,
        item_id: str,
        new_path: str | os.PathLike[str],
        *,
        item_type: ItemType | None = None,
    ) -> LauncherItem:
        path_text = _optional_text(new_path)
        if not path_text:
            raise ValueError("Replacement path must not be empty")
        item = self.get_item(item_id)
        item.path = path_text
        item.type = (
            item_type
            if item_type in ("folder", "file", "url")
            else detect_item_type(path_text)
        )
        if item.type == "url":
            item.path = normalize_web_url(item.path)
        return item

    def set_columns(self, columns: int) -> int:
        self.columns = _coerce_columns(columns)
        return self.columns

    def set_appearance_mode(self, mode: str) -> AppearanceMode:
        self.appearance_mode = _coerce_appearance_mode(mode)
        return self.appearance_mode

    # Short aliases keep controller code expressive while retaining explicit
    # method names for callers that prefer them.
    def add(self, *args: Any, **kwargs: Any) -> LauncherItem:
        return self.add_item(*args, **kwargs)

    def delete(self, item_id: str) -> LauncherItem:
        return self.delete_item(item_id)

    def rename(self, item_id: str, new_name: str) -> LauncherItem:
        return self.rename_item(item_id, new_name)

    def move(self, item_id: str, new_index: int) -> LauncherItem:
        return self.move_item(item_id, new_index)

    def replace_item_path(
        self,
        item_id: str,
        new_path: str | os.PathLike[str],
        *,
        item_type: ItemType | None = None,
    ) -> LauncherItem:
        return self.replace_path(item_id, new_path, item_type=item_type)
