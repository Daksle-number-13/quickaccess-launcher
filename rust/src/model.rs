//! Domain model: schema migration, invariants and stable ordering.
//!
//! The on-disk format is shared with the Python releases, so a config written
//! by either implementation can be read by the other.

use std::fmt;

use serde_json::{json, Map, Value};

use crate::pyish::{display_name, is_space, strip};
use crate::url;

pub const CURRENT_SCHEMA_VERSION: i64 = 2;
pub const DEFAULT_HOTKEY: &str = "ctrl+space";
pub const DEFAULT_QUICK_ADD_HOTKEY: &str = "ctrl+shift+space";
pub const DEFAULT_COLUMNS: i64 = 3;
pub const MIN_COLUMNS: i64 = 2;
pub const MAX_COLUMNS: i64 = 5;
pub const MAX_LAUNCHER_ITEMS: usize = 200;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// A newer app owns this file; it must never be treated as corrupt.
    UnsupportedSchema(i64),
    ItemLimit(usize),
    Invalid(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedSchema(version) => write!(
                f,
                "Configuration schema version {version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
            ),
            Self::ItemLimit(count) => write!(
                f,
                "바로가기는 최대 {MAX_LAUNCHER_ITEMS}개까지 등록할 수 있습니다 (요청: {count}개)"
            ),
            Self::Invalid(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for ConfigError {}

fn invalid(message: &str) -> ConfigError {
    ConfigError::Invalid(message.to_string())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ItemType {
    Folder,
    File,
    Url,
}

impl ItemType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Folder => "folder",
            Self::File => "file",
            Self::Url => "url",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "folder" => Some(Self::Folder),
            "file" => Some(Self::File),
            "url" => Some(Self::Url),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppearanceMode {
    System,
    Light,
    Dark,
}

impl AppearanceMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::Light => "light",
            Self::Dark => "dark",
        }
    }

    fn coerce(value: Option<&Value>) -> Self {
        match optional_text(value).to_lowercase().as_str() {
            "light" => Self::Light,
            "dark" => Self::Dark,
            _ => Self::System,
        }
    }
}

fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

fn optional_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => strip(text).to_string(),
        _ => String::new(),
    }
}

/// CPython's `int(value)` for JSON input, excluding booleans.
fn py_int(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Some(int)
            } else {
                let float = number.as_f64()?;
                (float.is_finite() && float.abs() < 9.2e18).then(|| float.trunc() as i64)
            }
        }
        Value::String(text) => {
            let trimmed = text.trim_matches(is_space);
            let digits = trimmed.strip_prefix(['+', '-']).unwrap_or(trimmed);
            let valid = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
            if !valid {
                return None;
            }
            trimmed.replace('_', "").parse().ok()
        }
        _ => None,
    }
}

fn coerce_order(value: Option<&Value>, fallback: i64) -> i64 {
    match py_int(value) {
        Some(parsed) if parsed >= 0 => parsed,
        _ => fallback,
    }
}

fn clamp_columns(columns: i64) -> i64 {
    columns.clamp(MIN_COLUMNS, MAX_COLUMNS)
}

fn coerce_columns(value: Option<&Value>) -> i64 {
    py_int(value).map_or(DEFAULT_COLUMNS, clamp_columns)
}

fn coerce_bool(value: Option<&Value>, default: bool) -> bool {
    match value {
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => match number.as_i64() {
            Some(0) => false,
            Some(1) => true,
            _ => default,
        },
        Some(Value::String(text)) => match strip(text).to_lowercase().as_str() {
            "true" | "yes" | "on" | "1" => true,
            "false" | "no" | "off" | "0" => false,
            _ => default,
        },
        _ => default,
    }
}

fn coerce_hotkey(value: Option<&Value>, default: &str) -> String {
    let text = optional_text(value);
    if text.is_empty() {
        default.to_string()
    } else {
        text.to_lowercase()
    }
}

/// Classify a path by touching the filesystem, as user-driven adds require.
pub fn detect_item_type(path: &str) -> ItemType {
    if std::fs::metadata(path).map(|meta| meta.is_dir()).unwrap_or(false) {
        ItemType::Folder
    } else {
        ItemType::File
    }
}

/// Classify legacy data without touching a potentially blocking path.
fn migrate_item_type(path: &str) -> ItemType {
    match url::split(path) {
        Ok(parts) if parts.scheme == "http" || parts.scheme == "https" => ItemType::Url,
        _ => ItemType::File,
    }
}

/// Return a safe HTTP(S) URL, adding HTTPS when the scheme is omitted.
pub fn normalize_web_url(value: &str) -> Result<String, ConfigError> {
    let text = strip(value);
    if text.is_empty() {
        return Err(invalid("Web URL must not be empty"));
    }
    if text
        .chars()
        .any(|c| is_space(c) || (c as u32) < 32 || c == '\\')
    {
        return Err(invalid(
            "Web URL must not contain whitespace or control characters",
        ));
    }
    let candidate = if text.contains("://") {
        text.to_string()
    } else {
        format!("https://{text}")
    };
    let parts = url::split(&candidate).map_err(|()| invalid("Invalid web URL"))?;
    parts.port().map_err(|()| invalid("Invalid web URL"))?;
    if (parts.scheme != "http" && parts.scheme != "https") || parts.hostname().is_empty() {
        return Err(invalid("Only HTTP and HTTPS web URLs are supported"));
    }
    if parts.has_credentials() {
        return Err(invalid("Web URLs containing credentials are not supported"));
    }
    Ok(parts.unsplit())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LauncherItem {
    pub id: String,
    pub name: String,
    pub path: String,
    pub item_type: ItemType,
    pub order: i64,
}

impl LauncherItem {
    pub fn new(
        id: &str,
        name: &str,
        path: &str,
        item_type: ItemType,
        order: i64,
    ) -> Result<Self, ConfigError> {
        let mut path = strip(path).to_string();
        if path.is_empty() {
            return Err(invalid("Launcher item path must not be empty"));
        }
        let trimmed_name = strip(name);
        let name = if trimmed_name.is_empty() {
            display_name(&path)
        } else {
            trimmed_name.to_string()
        };
        let trimmed_id = strip(id);
        let id = if trimmed_id.is_empty() {
            new_id()
        } else {
            trimmed_id.to_string()
        };
        if item_type == ItemType::Url {
            path = normalize_web_url(&path)?;
        }
        Ok(Self {
            id,
            name,
            path,
            item_type,
            order: order.max(0),
        })
    }

    fn from_json(data: &Map<String, Value>, fallback_order: i64) -> Result<Self, ConfigError> {
        let path = optional_text(data.get("path"));
        if path.is_empty() {
            return Err(invalid("Launcher item is missing a path"));
        }
        let item_type = data
            .get("type")
            .and_then(Value::as_str)
            .and_then(ItemType::parse)
            .unwrap_or_else(|| migrate_item_type(&path));
        Self::new(
            &optional_text(data.get("id")),
            &optional_text(data.get("name")),
            &path,
            item_type,
            coerce_order(data.get("order"), fallback_order),
        )
    }

    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "type": self.item_type.as_str(),
            "order": self.order,
        })
    }
}

/// Where the default Downloads and Documents items point.
#[derive(Debug, Clone)]
pub struct DefaultFolders {
    pub downloads: String,
    pub documents: String,
}

impl DefaultFolders {
    /// Plain children of `home`, which is what imports and tests rely on.
    pub fn under(home: &str) -> Self {
        Self {
            downloads: format!("{home}\\Downloads"),
            documents: format!("{home}\\Documents"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LauncherConfig {
    pub hotkey: String,
    pub quick_add_hotkey: String,
    pub appearance_mode: AppearanceMode,
    pub run_on_startup: bool,
    pub columns: i64,
    pub welcome_shown: bool,
    pub check_updates: bool,
    pub last_update_notice: String,
    pub items: Vec<LauncherItem>,
}

impl LauncherConfig {
    pub fn empty() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.to_string(),
            quick_add_hotkey: DEFAULT_QUICK_ADD_HOTKEY.to_string(),
            appearance_mode: AppearanceMode::System,
            run_on_startup: true,
            columns: DEFAULT_COLUMNS,
            welcome_shown: false,
            check_updates: false,
            last_update_notice: String::new(),
            items: Vec::new(),
        }
    }

    pub fn default_with(folders: &DefaultFolders) -> Self {
        let mut config = Self::empty();
        config.items = vec![
            LauncherItem::new("", "다운로드", &folders.downloads, ItemType::Folder, 0)
                .expect("default path is non-empty"),
            LauncherItem::new("", "문서", &folders.documents, ItemType::Folder, 1)
                .expect("default path is non-empty"),
        ];
        config
    }

    /// Parse the current object schema or migrate the legacy top-level list.
    ///
    /// Missing item fields are repaired. Entries without a usable path are
    /// skipped, because they could never be launched or repaired.
    pub fn from_value(data: &Value, folders: &DefaultFolders) -> Result<Self, ConfigError> {
        let mut config = Self::empty();
        let raw_items: Vec<Value>;
        match data {
            Value::Object(object) => {
                read_schema_version(object)?;
                raw_items = match object.get("items") {
                    Some(Value::Array(items)) => items.clone(),
                    Some(_) => return Err(invalid("Configuration 'items' must be a JSON array")),
                    None => Self::default_with(folders)
                        .items
                        .iter()
                        .map(LauncherItem::to_json)
                        .collect(),
                };
                config.hotkey = coerce_hotkey(object.get("hotkey"), DEFAULT_HOTKEY);
                config.quick_add_hotkey =
                    coerce_hotkey(object.get("quick_add_hotkey"), DEFAULT_QUICK_ADD_HOTKEY);
                config.appearance_mode = AppearanceMode::coerce(object.get("appearance_mode"));
                config.run_on_startup = coerce_bool(object.get("run_on_startup"), true);
                config.columns = coerce_columns(object.get("columns"));
                config.welcome_shown = coerce_bool(object.get("welcome_shown"), false);
                config.check_updates = coerce_bool(object.get("check_updates"), false);
                config.last_update_notice = optional_text(object.get("last_update_notice"));
            }
            Value::Array(items) => raw_items = items.clone(),
            _ => return Err(invalid("Configuration root must be a JSON object or array")),
        }
        if raw_items.len() > MAX_LAUNCHER_ITEMS {
            return Err(ConfigError::ItemLimit(raw_items.len()));
        }
        for (index, raw_item) in raw_items.iter().enumerate() {
            if let Value::Object(object) = raw_item {
                if let Ok(item) = LauncherItem::from_json(object, index as i64) {
                    config.items.push(item);
                }
            }
        }
        config.normalize()?;
        Ok(config)
    }

    /// Restore all config invariants without usage-based reordering.
    pub fn normalize(&mut self) -> Result<(), ConfigError> {
        if self.items.len() > MAX_LAUNCHER_ITEMS {
            return Err(ConfigError::ItemLimit(self.items.len()));
        }
        self.columns = clamp_columns(self.columns);
        // `sort_by_key` is stable, which preserves input order for equal keys.
        self.items.sort_by_key(|item| item.order);
        let mut seen = std::collections::HashSet::new();
        for (index, item) in self.items.iter_mut().enumerate() {
            while item.id.is_empty() || seen.contains(&item.id) {
                item.id = new_id();
            }
            seen.insert(item.id.clone());
            item.order = index as i64;
        }
        Ok(())
    }

    pub fn to_value(&self) -> Value {
        json!({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "hotkey": self.hotkey,
            "quick_add_hotkey": self.quick_add_hotkey,
            "appearance_mode": self.appearance_mode.as_str(),
            "run_on_startup": self.run_on_startup,
            "columns": self.columns,
            "welcome_shown": self.welcome_shown,
            "check_updates": self.check_updates,
            "last_update_notice": self.last_update_notice,
            "items": self.items.iter().map(LauncherItem::to_json).collect::<Vec<_>>(),
        })
    }

    pub fn get_item(&self, item_id: &str) -> Option<&LauncherItem> {
        self.items.iter().find(|item| item.id == item_id)
    }

    fn require_index(&self, item_id: &str) -> Result<usize, ConfigError> {
        self.items
            .iter()
            .position(|item| item.id == item_id)
            .ok_or_else(|| ConfigError::Invalid(format!("Unknown launcher item id: {item_id}")))
    }

    fn renumber(&mut self) {
        for (index, item) in self.items.iter_mut().enumerate() {
            item.order = index as i64;
        }
    }

    /// Add an item; a `None` type classifies the path on disk.
    pub fn add_item(
        &mut self,
        path: &str,
        name: &str,
        item_type: Option<ItemType>,
        position: Option<i64>,
    ) -> Result<String, ConfigError> {
        if self.items.len() >= MAX_LAUNCHER_ITEMS {
            return Err(ConfigError::ItemLimit(self.items.len() + 1));
        }
        let path_text = strip(path);
        if path_text.is_empty() {
            return Err(invalid("Launcher item path must not be empty"));
        }
        let resolved = item_type.unwrap_or_else(|| detect_item_type(path_text));
        let item = LauncherItem::new("", name, path_text, resolved, self.items.len() as i64)?;
        let id = item.id.clone();
        match position {
            None => self.items.push(item),
            Some(position) => {
                let bounded = position.clamp(0, self.items.len() as i64) as usize;
                self.items.insert(bounded, item);
            }
        }
        self.renumber();
        self.normalize()?;
        Ok(id)
    }

    pub fn delete_item(&mut self, item_id: &str) -> Result<LauncherItem, ConfigError> {
        let index = self.require_index(item_id)?;
        let item = self.items.remove(index);
        self.normalize()?;
        Ok(item)
    }

    pub fn rename_item(&mut self, item_id: &str, new_name: &str) -> Result<(), ConfigError> {
        let name = strip(new_name);
        if name.is_empty() {
            return Err(invalid("Launcher item name must not be empty"));
        }
        let index = self.require_index(item_id)?;
        self.items[index].name = name.to_string();
        Ok(())
    }

    pub fn move_item(&mut self, item_id: &str, new_index: i64) -> Result<(), ConfigError> {
        let old_index = self.require_index(item_id)?;
        let item = self.items.remove(old_index);
        let bounded = new_index.clamp(0, self.items.len() as i64) as usize;
        self.items.insert(bounded, item);
        self.renumber();
        self.normalize()
    }

    pub fn replace_path(
        &mut self,
        item_id: &str,
        new_path: &str,
        item_type: Option<ItemType>,
    ) -> Result<(), ConfigError> {
        let path_text = strip(new_path);
        if path_text.is_empty() {
            return Err(invalid("Replacement path must not be empty"));
        }
        let index = self.require_index(item_id)?;
        let resolved = item_type.unwrap_or_else(|| detect_item_type(path_text));
        let stored = if resolved == ItemType::Url {
            normalize_web_url(path_text)?
        } else {
            path_text.to_string()
        };
        let item = &mut self.items[index];
        item.item_type = resolved;
        item.path = stored;
        Ok(())
    }
}

fn read_schema_version(data: &Map<String, Value>) -> Result<Option<i64>, ConfigError> {
    let Some(value) = data.get("schema_version") else {
        return Ok(None);
    };
    let error = || invalid("Configuration 'schema_version' must be a positive integer");
    let version = value
        .as_i64()
        .filter(|version| *version >= 1)
        .ok_or_else(error)?;
    if version > CURRENT_SCHEMA_VERSION {
        return Err(ConfigError::UnsupportedSchema(version));
    }
    Ok(Some(version))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folders() -> DefaultFolders {
        DefaultFolders::under(r"C:\Users\tester")
    }

    fn parse(text: &str) -> Result<LauncherConfig, ConfigError> {
        LauncherConfig::from_value(&serde_json::from_str(text).unwrap(), &folders())
    }

    #[test]
    fn default_config_has_two_folders() {
        let config = LauncherConfig::default_with(&folders());
        assert_eq!(config.items.len(), 2);
        assert_eq!(config.items[0].name, "다운로드");
        assert_eq!(config.items[0].path, r"C:\Users\tester\Downloads");
        assert_eq!(config.items[1].item_type, ItemType::Folder);
        assert_ne!(config.items[0].id, config.items[1].id);
        assert!(config.run_on_startup);
        assert!(!config.check_updates);
    }

    #[test]
    fn migrates_the_legacy_top_level_list() {
        let config = parse(
            r#"[{"name":"A","path":"C:\\a"},{"path":"https://example.com"},{"name":"no path"},7]"#,
        )
        .unwrap();
        assert_eq!(config.items.len(), 2);
        assert_eq!(config.items[0].item_type, ItemType::File);
        assert_eq!(config.items[1].item_type, ItemType::Url);
        assert_eq!(config.items[1].name, "example.com");
    }

    #[test]
    fn missing_items_key_yields_defaults() {
        assert_eq!(parse(r#"{"columns":4}"#).unwrap().items.len(), 2);
    }

    #[test]
    fn rejects_unusable_roots_and_item_shapes() {
        assert!(parse("3").is_err());
        assert!(parse(r#"{"items":"x"}"#).is_err());
        assert!(parse(r#"{"schema_version":0,"items":[]}"#).is_err());
        assert!(parse(r#"{"schema_version":true,"items":[]}"#).is_err());
        assert!(parse(r#"{"schema_version":1.0,"items":[]}"#).is_err());
    }

    #[test]
    fn newer_schema_is_a_distinct_error() {
        assert_eq!(
            parse(r#"{"schema_version":3,"items":[]}"#),
            Err(ConfigError::UnsupportedSchema(3))
        );
    }

    #[test]
    fn enforces_the_item_limit() {
        let items: Vec<Value> = (0..201).map(|i| json!({"path": format!("C:\\{i}")})).collect();
        let result = LauncherConfig::from_value(&Value::Array(items), &folders());
        assert_eq!(result, Err(ConfigError::ItemLimit(201)));

        let mut config = LauncherConfig::empty();
        for index in 0..MAX_LAUNCHER_ITEMS {
            config
                .add_item(&format!("C:\\{index}"), "x", Some(ItemType::File), None)
                .unwrap();
        }
        assert!(matches!(
            config.add_item("C:\\x", "x", Some(ItemType::File), None),
            Err(ConfigError::ItemLimit(201))
        ));
    }

    #[test]
    fn coerces_scalar_settings_like_python() {
        let config = parse(
            r#"{"hotkey":" CTRL+K ","quick_add_hotkey":"","appearance_mode":"DARK",
                "run_on_startup":"off","columns":"9","welcome_shown":1,
                "check_updates":"maybe","last_update_notice":" v1 ","items":[]}"#,
        )
        .unwrap();
        assert_eq!(config.hotkey, "ctrl+k");
        assert_eq!(config.quick_add_hotkey, DEFAULT_QUICK_ADD_HOTKEY);
        assert_eq!(config.appearance_mode, AppearanceMode::Dark);
        assert!(!config.run_on_startup);
        assert_eq!(config.columns, MAX_COLUMNS);
        assert!(config.welcome_shown);
        assert!(!config.check_updates);
        assert_eq!(config.last_update_notice, "v1");

        assert_eq!(parse(r#"{"columns":true,"items":[]}"#).unwrap().columns, 3);
        assert_eq!(parse(r#"{"columns":1,"items":[]}"#).unwrap().columns, 2);
        assert_eq!(parse(r#"{"columns":4.9,"items":[]}"#).unwrap().columns, 4);
    }

    #[test]
    fn orders_items_stably_and_repairs_ids() {
        let config = parse(
            r#"{"items":[
                {"id":"a","path":"C:\\a","order":5},
                {"id":"a","path":"C:\\b","order":1},
                {"id":"c","path":"C:\\c","order":5},
                {"path":"C:\\d","order":-3},
                {"id":"e","path":"C:\\e","order":true}
            ]}"#,
        )
        .unwrap();
        let paths: Vec<&str> = config.items.iter().map(|item| item.path.as_str()).collect();
        assert_eq!(paths, [r"C:\b", r"C:\d", r"C:\e", r"C:\a", r"C:\c"]);
        let orders: Vec<i64> = config.items.iter().map(|item| item.order).collect();
        assert_eq!(orders, [0, 1, 2, 3, 4]);
        let unique: std::collections::HashSet<_> =
            config.items.iter().map(|item| item.id.clone()).collect();
        assert_eq!(unique.len(), 5);
    }

    #[test]
    fn invalid_url_items_are_dropped() {
        let config = parse(
            r#"{"items":[{"type":"url","path":"ftp://x"},{"type":"url","path":"example.com"}]}"#,
        )
        .unwrap();
        assert_eq!(config.items.len(), 1);
        assert_eq!(config.items[0].path, "https://example.com");
    }

    #[test]
    fn normalizes_web_urls() {
        assert_eq!(normalize_web_url(" Example.com/a ").unwrap(), "https://Example.com/a");
        assert_eq!(normalize_web_url("HTTP://x.y/?q=1#f").unwrap(), "http://x.y/?q=1#f");
        for bad in [
            "",
            "ftp://x.y",
            "https://",
            "https://u:p@x.y",
            "https://x.y:abc",
            "a b.com",
            "a\\b.com",
            "mailto:a@b.c",
        ] {
            assert!(normalize_web_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn mutations_keep_order_contiguous() {
        let mut config = LauncherConfig::empty();
        let a = config.add_item(r"C:\a", "a", Some(ItemType::File), None).unwrap();
        let b = config.add_item(r"C:\b", "b", Some(ItemType::File), None).unwrap();
        let c = config.add_item(r"C:\c", "c", Some(ItemType::File), Some(0)).unwrap();
        assert_eq!(config.items[0].id, c);

        config.move_item(&a, 99).unwrap();
        let ids: Vec<&str> = config.items.iter().map(|item| item.id.as_str()).collect();
        assert_eq!(ids, [c.as_str(), b.as_str(), a.as_str()]);

        config.delete_item(&b).unwrap();
        assert_eq!(config.items[1].order, 1);
        assert!(config.delete_item("missing").is_err());

        config.rename_item(&a, "  새 이름 ").unwrap();
        assert_eq!(config.get_item(&a).unwrap().name, "새 이름");
        assert!(config.rename_item(&a, "   ").is_err());

        config.replace_path(&a, "example.com", Some(ItemType::Url)).unwrap();
        assert_eq!(config.get_item(&a).unwrap().path, "https://example.com");
        assert!(config.replace_path(&a, "ftp://x", Some(ItemType::Url)).is_err());
        assert_eq!(config.get_item(&a).unwrap().path, "https://example.com");
    }

    #[test]
    fn detects_type_from_disk_when_unspecified() {
        let directory = std::env::temp_dir();
        let mut config = LauncherConfig::empty();
        let id = config
            .add_item(directory.to_str().unwrap(), "", None, None)
            .unwrap();
        assert_eq!(config.get_item(&id).unwrap().item_type, ItemType::Folder);
        let id = config.add_item(r"C:\no\such\file.txt", "", None, None).unwrap();
        assert_eq!(config.get_item(&id).unwrap().item_type, ItemType::File);
        assert_eq!(config.get_item(&id).unwrap().name, "file.txt");
    }

    #[test]
    fn serializes_in_the_documented_field_order() {
        let mut config = LauncherConfig::empty();
        config.add_item(r"C:\a", "에이", Some(ItemType::File), None).unwrap();
        let value = config.to_value();
        let keys: Vec<&String> = value.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            [
                "schema_version",
                "hotkey",
                "quick_add_hotkey",
                "appearance_mode",
                "run_on_startup",
                "columns",
                "welcome_shown",
                "check_updates",
                "last_update_notice",
                "items"
            ]
        );
        let item = &value["items"][0];
        let item_keys: Vec<&String> = item.as_object().unwrap().keys().collect();
        assert_eq!(item_keys, ["id", "name", "path", "type", "order"]);
        assert_eq!(LauncherConfig::from_value(&value, &folders()).unwrap(), config);
    }
}
