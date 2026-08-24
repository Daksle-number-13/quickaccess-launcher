"""Thread-safe UTF-8 JSON persistence for QuickAccess configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Callable, TypeVar

from .models import LauncherConfig


_T = TypeVar("_T")

_REPLACE_RETRY_WINERRORS = frozenset({5, 32})
_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1)


def _atomic_replace(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> None:
    """Replace a file, retrying only transient Windows sharing failures.

    Antivirus scanners and sync clients can briefly hold a just-written file.
    Keep the retry window deliberately short and bounded so a real permission
    problem still reaches the caller promptly.
    """

    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            if winerror not in _REPLACE_RETRY_WINERRORS or attempt >= len(
                _REPLACE_RETRY_DELAYS
            ):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


@dataclass(frozen=True, slots=True)
class LoadResult:
    """The loaded config plus any recovery/migration action that occurred."""

    config: LauncherConfig
    created: bool = False
    recovered: bool = False
    migrated: bool = False
    repaired: bool = False
    restored_from_backup: bool = False
    backup_path: Path | None = None

    @property
    def changed_on_disk(self) -> bool:
        return self.created or self.recovered or self.migrated or self.repaired


class ConfigStore:
    """Own the single on-disk configuration file.

    Writes use a temporary file in the destination directory followed by
    ``os.replace``.  Consequently readers can observe either the complete old
    file or the complete new file, never a partially written JSON document.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        user_home: str | os.PathLike[str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else self.default_path()
        self._user_home = user_home
        self._lock = threading.RLock()

    @staticmethod
    def default_path() -> Path:
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata)
        else:
            # This fallback also makes development on non-Windows systems
            # deterministic; production Windows always supplies APPDATA.
            base = Path.home() / "AppData" / "Roaming"
        return base / "QuickAccess" / "items.json"

    def load(self) -> LoadResult:
        with self._lock:
            if not self.path.exists():
                config = LauncherConfig.default(user_home=self._user_home)
                self._save_unlocked(config)
                return LoadResult(config=config, created=True)

            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    raw_data = json.load(stream)
            except (json.JSONDecodeError, UnicodeError):
                return self._recover_unlocked()

            try:
                config = LauncherConfig.from_data(
                    raw_data,
                    user_home=self._user_home,
                )
            except (TypeError, ValueError):
                return self._recover_unlocked()

            canonical_data = config.to_dict()
            # JSON text comparison (with sorted object keys) distinguishes
            # schema-relevant types that Python equality merges, such as
            # integer 1 and boolean true.
            migrated = json.dumps(
                raw_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) != json.dumps(
                canonical_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            repaired = self._discarded_invalid_items(raw_data, config)
            backup_path: Path | None = None
            if repaired:
                backup_path = self._next_backup_path()
                shutil.copy2(self.path, backup_path)
            if migrated:
                self._save_unlocked(config)
            return LoadResult(
                config=config,
                migrated=migrated,
                repaired=repaired,
                backup_path=backup_path,
            )

    def load_config(self) -> LauncherConfig:
        """Convenience API for callers that do not need load metadata."""

        return self.load().config

    def save(self, config: LauncherConfig) -> LauncherConfig:
        if not isinstance(config, LauncherConfig):
            raise TypeError("ConfigStore.save expects LauncherConfig")
        with self._lock:
            config.normalize()
            self._save_unlocked(config)
            return config

    def update(
        self,
        mutator: Callable[[LauncherConfig], _T],
    ) -> tuple[LauncherConfig, _T]:
        """Load, mutate, and save while holding the store's re-entrant lock."""

        with self._lock:
            config = self.load().config
            result = mutator(config)
            self._save_unlocked(config)
            return config, result

    def _recover_unlocked(self) -> LoadResult:
        backup_path = self._next_backup_path()
        _atomic_replace(self.path, backup_path)
        config = self._load_rolling_backup_unlocked()
        restored_from_backup = config is not None
        if config is None:
            config = LauncherConfig.default(user_home=self._user_home)
        self._save_unlocked(config)
        return LoadResult(
            config=config,
            recovered=True,
            restored_from_backup=restored_from_backup,
            backup_path=backup_path,
        )

    def _load_rolling_backup_unlocked(self) -> LauncherConfig | None:
        """Return the last known-good config when its JSON and schema are valid."""

        backup_path = self._rolling_backup_path()
        if not backup_path.is_file():
            return None
        try:
            with backup_path.open("r", encoding="utf-8") as stream:
                raw_data = json.load(stream)
            return LauncherConfig.from_data(raw_data, user_home=self._user_home)
        except (OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
            return None

    def _next_backup_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        suffix = self.path.suffix or ".json"
        stem = self.path.stem if self.path.suffix else self.path.name
        candidate = self.path.with_name(f"{stem}.corrupt-{timestamp}{suffix}")
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(
                f"{stem}.corrupt-{timestamp}-{counter}{suffix}"
            )
            counter += 1
        return candidate

    @staticmethod
    def _discarded_invalid_items(raw_data: object, config: LauncherConfig) -> bool:
        """Detect parser repairs that removed entries and preserve their source."""

        raw_items: object
        if isinstance(raw_data, dict):
            raw_items = raw_data.get("items", [])
        elif isinstance(raw_data, list):
            raw_items = raw_data
        else:
            return False
        return isinstance(raw_items, list) and len(raw_items) != len(config.items)

    def _rolling_backup_path(self) -> Path:
        suffix = self.path.suffix or ".json"
        stem = self.path.stem if self.path.suffix else self.path.name
        return self.path.with_name(f"{stem}.bak{suffix}")

    def _save_unlocked(self, config: LauncherConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.to_dict()

        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(
                file_descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                # Best-effort single rolling backup of the previous version so
                # an accidental bad edit can be recovered from the file system
                # without blocking or failing the actual save.
                try:
                    shutil.copy2(self.path, self._rolling_backup_path())
                except OSError:
                    pass
            _atomic_replace(temporary_path, self.path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # A failed cleanup must not mask the persistence error that led
                # here.  Normal successful writes have already moved the file.
                pass
