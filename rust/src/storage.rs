//! UTF-8 JSON persistence with atomic replacement and bounded recovery.

use std::fmt;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::model::{ConfigError, DefaultFolders, LauncherConfig};

pub const MAX_CONFIG_BYTES: u64 = 2 * 1024 * 1024;
const DEFAULT_MAX_CORRUPT_BACKUPS: usize = 5;
const REPLACE_RETRY_WINERRORS: [i32; 2] = [5, 32];
const REPLACE_RETRY_DELAYS_MS: [u64; 3] = [20, 50, 100];

#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Config(ConfigError),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "{error}"),
            Self::Config(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<io::Error> for StoreError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<ConfigError> for StoreError {
    fn from(error: ConfigError) -> Self {
        Self::Config(error)
    }
}

/// Replace a file, retrying only transient Windows sharing failures.
///
/// Antivirus scanners and sync clients can briefly hold a just-written file.
/// The retry window stays short so a real permission problem still surfaces.
pub fn atomic_replace(source: &Path, destination: &Path) -> io::Result<()> {
    let mut attempt = 0;
    loop {
        match fs::rename(source, destination) {
            Ok(()) => return Ok(()),
            Err(error) => {
                let retryable = error
                    .raw_os_error()
                    .is_some_and(|code| REPLACE_RETRY_WINERRORS.contains(&code));
                if !retryable || attempt >= REPLACE_RETRY_DELAYS_MS.len() {
                    return Err(error);
                }
                std::thread::sleep(Duration::from_millis(REPLACE_RETRY_DELAYS_MS[attempt]));
                attempt += 1;
            }
        }
    }
}

/// Write `payload` to a sibling temp file, flush it, then swap it into place.
///
/// `before_replace` runs after the data is durable and before the swap.
pub fn write_atomically(
    path: &Path,
    payload: &str,
    before_replace: impl FnOnce(),
) -> io::Result<()> {
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty());
    let directory = parent.unwrap_or_else(|| Path::new("."));
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (mut file, temporary) = create_temporary(directory, &file_name)?;
    let result = (|| {
        file.write_all(payload.as_bytes())?;
        file.flush()?;
        file.sync_all()?;
        drop(file);
        before_replace();
        atomic_replace(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn create_temporary(directory: &Path, file_name: &str) -> io::Result<(fs::File, PathBuf)> {
    for _ in 0..100 {
        let token = uuid::Uuid::new_v4().simple().to_string();
        let candidate = directory.join(format!(".{file_name}.{}.tmp", &token[..8]));
        match fs::OpenOptions::new().write(true).create_new(true).open(&candidate) {
            Ok(file) => return Ok((file, candidate)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "could not create a unique temporary file",
    ))
}

/// The loaded config plus any recovery or migration action that occurred.
#[derive(Debug)]
pub struct LoadResult {
    pub config: LauncherConfig,
    pub created: bool,
    pub recovered: bool,
    pub migrated: bool,
    pub repaired: bool,
    pub restored_from_backup: bool,
    pub backup_path: Option<PathBuf>,
}

impl LoadResult {
    fn new(config: LauncherConfig) -> Self {
        Self {
            config,
            created: false,
            recovered: false,
            migrated: false,
            repaired: false,
            restored_from_backup: false,
            backup_path: None,
        }
    }
}

enum ReadFailure {
    Io(io::Error),
    /// Invalid JSON or UTF-8, or more than the bounded read limit.
    Corrupt,
}

fn read_json(path: &Path) -> Result<Value, ReadFailure> {
    let file = fs::File::open(path).map_err(ReadFailure::Io)?;
    let mut source = Vec::new();
    file.take(MAX_CONFIG_BYTES + 1)
        .read_to_end(&mut source)
        .map_err(ReadFailure::Io)?;
    if source.len() as u64 > MAX_CONFIG_BYTES {
        return Err(ReadFailure::Corrupt);
    }
    let text = std::str::from_utf8(&source).map_err(|_| ReadFailure::Corrupt)?;
    serde_json::from_str(text).map_err(|_| ReadFailure::Corrupt)
}

/// `%Y%m%d-%H%M%S-%f` in UTC, used only to order and distinguish snapshots.
fn snapshot_timestamp() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let (year, month, day, hour, minute, second) = civil_from_unix(elapsed.as_secs() as i64);
    format!(
        "{year:04}{month:02}{day:02}-{hour:02}{minute:02}{second:02}-{:06}",
        elapsed.subsec_micros()
    )
}

/// Days-to-civil conversion from Howard Hinnant's public-domain algorithm.
pub fn civil_from_unix(seconds: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = seconds.div_euclid(86_400);
    let remainder = seconds.rem_euclid(86_400) as u32;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (
        year,
        month,
        day,
        remainder / 3_600,
        remainder % 3_600 / 60,
        remainder % 60,
    )
}

/// Owns the single on-disk configuration file.
pub struct ConfigStore {
    pub path: PathBuf,
    folders: DefaultFolders,
    max_corrupt_backups: usize,
}

impl ConfigStore {
    pub fn new(path: PathBuf, folders: DefaultFolders) -> Self {
        Self {
            path,
            folders,
            max_corrupt_backups: DEFAULT_MAX_CORRUPT_BACKUPS,
        }
    }

    #[cfg(test)]
    fn with_backup_limit(mut self, limit: usize) -> Self {
        self.max_corrupt_backups = limit;
        self
    }

    pub fn default_path() -> PathBuf {
        let base = std::env::var_os("APPDATA")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("USERPROFILE").map(|home| {
                    let mut path = PathBuf::from(home);
                    path.push("AppData");
                    path.push("Roaming");
                    path
                })
            })
            .unwrap_or_else(|| PathBuf::from("."));
        base.join("QuickAccess").join("items.json")
    }

    fn stem_and_suffix(&self) -> (String, String) {
        let name = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        match self.path.extension() {
            Some(extension) => {
                let stem = self
                    .path
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default();
                (stem, format!(".{}", extension.to_string_lossy()))
            }
            None => (name, ".json".to_string()),
        }
    }

    pub fn rolling_backup_path(&self) -> PathBuf {
        let (stem, suffix) = self.stem_and_suffix();
        self.path.with_file_name(format!("{stem}.bak{suffix}"))
    }

    fn next_backup_path(&self) -> PathBuf {
        let (stem, suffix) = self.stem_and_suffix();
        let timestamp = snapshot_timestamp();
        let mut candidate = self
            .path
            .with_file_name(format!("{stem}.corrupt-{timestamp}{suffix}"));
        let mut counter = 1;
        while candidate.exists() {
            candidate = self
                .path
                .with_file_name(format!("{stem}.corrupt-{timestamp}-{counter}{suffix}"));
            counter += 1;
        }
        candidate
    }

    pub fn load(&self) -> Result<LoadResult, StoreError> {
        if !self.path.exists() {
            if let Some(config) = self.load_rolling_backup() {
                self.write(&config)?;
                let mut result = LoadResult::new(config);
                result.recovered = true;
                result.restored_from_backup = true;
                result.backup_path = Some(self.rolling_backup_path());
                return Ok(result);
            }
            let config = LauncherConfig::default_with(&self.folders);
            self.write(&config)?;
            let mut result = LoadResult::new(config);
            result.created = true;
            return Ok(result);
        }

        let raw = match read_json(&self.path) {
            Ok(raw) => raw,
            Err(ReadFailure::Corrupt) => return self.recover(),
            Err(ReadFailure::Io(error)) => return Err(error.into()),
        };
        let config = match LauncherConfig::from_value(&raw, &self.folders) {
            Ok(config) => config,
            // A newer app owns this valid file. Moving or rewriting it as if
            // it were corrupt would destroy data during a downgrade.
            Err(error @ ConfigError::UnsupportedSchema(_)) => return Err(error.into()),
            Err(_) => return self.recover(),
        };

        // `Value` equality keeps integer 1 and boolean true distinct and
        // ignores object key order.
        let migrated = raw != config.to_value();
        let repaired = discarded_invalid_items(&raw, &config);
        let mut backup_path = None;
        if repaired {
            let snapshot = self.next_backup_path();
            fs::copy(&self.path, &snapshot)?;
            backup_path = Some(snapshot);
        }
        if migrated {
            self.write(&config)?;
        }
        if let Some(keep) = &backup_path {
            self.prune_corrupt_backups(keep);
        }
        let mut result = LoadResult::new(config);
        result.migrated = migrated;
        result.repaired = repaired;
        result.backup_path = backup_path;
        Ok(result)
    }

    pub fn save(&self, config: &mut LauncherConfig) -> Result<(), StoreError> {
        config.normalize()?;
        self.write(config)
    }

    fn recover(&self) -> Result<LoadResult, StoreError> {
        let backup_path = self.next_backup_path();
        // Detach the invalid primary atomically before writing its replacement.
        // If that save fails, the next launch sees a missing primary and can
        // restore the untouched rolling backup before considering defaults.
        atomic_replace(&self.path, &backup_path)?;
        let restored = self.load_rolling_backup();
        let restored_from_backup = restored.is_some();
        let config = restored.unwrap_or_else(|| LauncherConfig::default_with(&self.folders));
        self.write(&config)?;
        self.prune_corrupt_backups(&backup_path);
        let mut result = LoadResult::new(config);
        result.recovered = true;
        result.restored_from_backup = restored_from_backup;
        result.backup_path = Some(backup_path);
        Ok(result)
    }

    fn load_rolling_backup(&self) -> Option<LauncherConfig> {
        let path = self.rolling_backup_path();
        if !path.is_file() {
            return None;
        }
        let raw = read_json(&path).ok()?;
        LauncherConfig::from_value(&raw, &self.folders).ok()
    }

    /// Best-effort retention for generated corrupt snapshots.
    ///
    /// The rolling `.bak` file has a distinct name and is never in this set.
    /// `keep` protects the snapshot created for the current recovery.
    fn prune_corrupt_backups(&self, keep: &Path) {
        let (stem, suffix) = self.stem_and_suffix();
        let prefix = format!("{stem}.corrupt-");
        let Some(parent) = self.path.parent() else {
            return;
        };
        let Ok(entries) = fs::read_dir(parent) else {
            return;
        };
        let mut backups: Vec<PathBuf> = entries
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| {
                path.is_file()
                    && path
                        .file_name()
                        .map(|name| name.to_string_lossy())
                        .is_some_and(|name| name.starts_with(&prefix) && name.ends_with(&suffix))
            })
            .collect();
        backups.sort_by_key(|path| path.file_name().map(|name| name.to_os_string()));
        let mut excess = backups.len() as i64 - self.max_corrupt_backups as i64;
        for candidate in backups {
            if excess <= 0 {
                break;
            }
            if candidate == keep {
                continue;
            }
            if fs::remove_file(&candidate).is_ok() {
                excess -= 1;
            }
        }
    }

    /// Avoid replacing the last-known-good backup with damaged input.
    fn primary_is_valid_backup_source(&self) -> bool {
        let Ok(raw) = read_json(&self.path) else {
            return false;
        };
        let Ok(parsed) = LauncherConfig::from_value(&raw, &self.folders) else {
            return false;
        };
        !discarded_invalid_items(&raw, &parsed)
    }

    fn write(&self, config: &LauncherConfig) -> Result<(), StoreError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let payload = render_json(&config.to_value());
        write_atomically(&self.path, &payload, || {
            // Best-effort rolling backup of the previous version. A corrupt
            // primary must never overwrite the last-known-good backup.
            if self.path.exists() && self.primary_is_valid_backup_source() {
                let _ = fs::copy(&self.path, self.rolling_backup_path());
            }
        })?;
        Ok(())
    }
}

/// Render exactly as `json.dump(indent=2, ensure_ascii=False)` plus a newline.
pub fn render_json(value: &Value) -> String {
    let mut text = serde_json::to_string_pretty(value).expect("a JSON value always serializes");
    text.push('\n');
    text
}

fn discarded_invalid_items(raw: &Value, config: &LauncherConfig) -> bool {
    let raw_count = match raw {
        Value::Object(object) => match object.get("items") {
            Some(Value::Array(items)) => items.len(),
            Some(_) => return false,
            None => 0,
        },
        Value::Array(items) => items.len(),
        _ => return false,
    };
    raw_count != config.items.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ItemType;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "quickaccess-test-{}",
                uuid::Uuid::new_v4().simple()
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn store(&self) -> ConfigStore {
            ConfigStore::new(
                self.0.join("items.json"),
                DefaultFolders::under(r"C:\Users\tester"),
            )
        }

        fn names(&self) -> Vec<String> {
            let mut names: Vec<String> = fs::read_dir(&self.0)
                .unwrap()
                .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            names.sort();
            names
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn creates_defaults_on_first_load() {
        let dir = TempDir::new();
        let result = dir.store().load().unwrap();
        assert!(result.created);
        assert_eq!(result.config.items.len(), 2);
        assert!(dir.0.join("items.json").is_file());
    }

    #[test]
    fn writes_python_compatible_utf8_json() {
        let dir = TempDir::new();
        let store = dir.store();
        let mut config = LauncherConfig::empty();
        config.add_item(r"C:\한글", "한글", Some(ItemType::Folder), None).unwrap();
        store.save(&mut config).unwrap();
        let text = fs::read_to_string(&store.path).unwrap();
        assert!(text.contains("\"name\": \"한글\""));
        assert!(text.starts_with("{\n  \"schema_version\": 2,\n  \"hotkey\""));
        assert!(text.ends_with("}\n"));
        assert!(!text.contains('\r'));
    }

    #[test]
    fn canonical_file_round_trips_without_migration() {
        let dir = TempDir::new();
        let store = dir.store();
        store.load().unwrap();
        let before = fs::read(&store.path).unwrap();
        let result = store.load().unwrap();
        assert!(!result.migrated && !result.repaired && !result.recovered);
        assert_eq!(fs::read(&store.path).unwrap(), before);
    }

    #[test]
    fn migrates_legacy_data_and_keeps_a_rolling_backup() {
        let dir = TempDir::new();
        let store = dir.store();
        fs::write(&store.path, r#"[{"name":"a","path":"C:\\a"}]"#).unwrap();
        let result = store.load().unwrap();
        assert!(result.migrated);
        assert!(!result.repaired);
        let saved: Value = serde_json::from_slice(&fs::read(&store.path).unwrap()).unwrap();
        assert_eq!(saved["schema_version"], 2);
        assert!(store.rolling_backup_path().is_file());
    }

    #[test]
    fn corrupt_file_is_snapshotted_and_defaults_written() {
        let dir = TempDir::new();
        let store = dir.store();
        fs::write(&store.path, "{ not json").unwrap();
        let result = store.load().unwrap();
        assert!(result.recovered);
        assert!(!result.restored_from_backup);
        let snapshot = result.backup_path.unwrap();
        assert_eq!(fs::read_to_string(snapshot).unwrap(), "{ not json");
        assert_eq!(result.config.items.len(), 2);
    }

    #[test]
    fn corrupt_file_restores_the_rolling_backup() {
        let dir = TempDir::new();
        let store = dir.store();
        let mut config = LauncherConfig::empty();
        config.add_item(r"C:\keep", "keep", Some(ItemType::File), None).unwrap();
        store.save(&mut config).unwrap();
        store.save(&mut config).unwrap();
        fs::write(&store.path, [0xFF, 0xFE]).unwrap();
        let result = store.load().unwrap();
        assert!(result.recovered && result.restored_from_backup);
        assert_eq!(result.config.items[0].name, "keep");
    }

    #[test]
    fn missing_primary_restores_from_backup_before_defaults() {
        let dir = TempDir::new();
        let store = dir.store();
        let mut config = LauncherConfig::empty();
        config.add_item(r"C:\keep", "keep", Some(ItemType::File), None).unwrap();
        store.save(&mut config).unwrap();
        store.save(&mut config).unwrap();
        fs::remove_file(&store.path).unwrap();
        let result = store.load().unwrap();
        assert!(result.recovered && result.restored_from_backup);
        assert_eq!(result.config.items.len(), 1);
    }

    #[test]
    fn oversized_file_is_treated_as_corrupt() {
        let dir = TempDir::new();
        let store = dir.store();
        fs::write(&store.path, vec![b' '; MAX_CONFIG_BYTES as usize + 1]).unwrap();
        assert!(store.load().unwrap().recovered);
    }

    #[test]
    fn newer_schema_is_left_untouched() {
        let dir = TempDir::new();
        let store = dir.store();
        let original = r#"{"schema_version":99,"items":[]}"#;
        fs::write(&store.path, original).unwrap();
        assert!(matches!(
            store.load(),
            Err(StoreError::Config(ConfigError::UnsupportedSchema(99)))
        ));
        assert_eq!(fs::read_to_string(&store.path).unwrap(), original);
        assert_eq!(dir.names(), ["items.json"]);
    }

    #[test]
    fn discarded_entries_preserve_the_source_snapshot() {
        let dir = TempDir::new();
        let store = dir.store();
        let original = r#"{"schema_version":2,"items":[{"name":"no path"},{"path":"C:\\a"}]}"#;
        fs::write(&store.path, original).unwrap();
        let result = store.load().unwrap();
        assert!(result.repaired && result.migrated);
        assert_eq!(
            fs::read_to_string(result.backup_path.unwrap()).unwrap(),
            original
        );
        assert_eq!(result.config.items.len(), 1);
    }

    #[test]
    fn damaged_primary_never_replaces_a_good_backup() {
        let dir = TempDir::new();
        let store = dir.store();
        let mut good = LauncherConfig::empty();
        good.add_item(r"C:\good", "good", Some(ItemType::File), None).unwrap();
        store.save(&mut good).unwrap();
        store.save(&mut good).unwrap();
        let backup = fs::read(store.rolling_backup_path()).unwrap();
        fs::write(&store.path, "broken").unwrap();
        store.save(&mut good).unwrap();
        assert_eq!(fs::read(store.rolling_backup_path()).unwrap(), backup);
    }

    #[test]
    fn prunes_old_corrupt_snapshots() {
        let dir = TempDir::new();
        let store = dir.store().with_backup_limit(2);
        for _ in 0..5 {
            fs::write(&store.path, "broken").unwrap();
            store.load().unwrap();
            std::thread::sleep(Duration::from_millis(2));
        }
        let snapshots = dir.names().into_iter().filter(|n| n.contains(".corrupt-")).count();
        assert_eq!(snapshots, 2);
    }

    #[test]
    fn leaves_no_temporary_files() {
        let dir = TempDir::new();
        let store = dir.store();
        store.load().unwrap();
        let mut config = LauncherConfig::empty();
        store.save(&mut config).unwrap();
        assert!(dir.names().iter().all(|name| !name.ends_with(".tmp")));
    }

    #[test]
    fn computes_civil_dates() {
        assert_eq!(civil_from_unix(0), (1970, 1, 1, 0, 0, 0));
        assert_eq!(civil_from_unix(1_788_566_400), (2026, 9, 5, 0, 0, 0));
        assert_eq!(civil_from_unix(951_782_399), (2000, 2, 28, 23, 59, 59));
    }
}
