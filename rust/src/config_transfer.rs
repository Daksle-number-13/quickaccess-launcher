//! Portable, side-effect-free configuration transfer.
//!
//! The import planner works on plain JSON. It never probes, opens or resolves
//! a launcher target, so previews stay responsive even when a configuration
//! contains disconnected network paths.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::path::Path;

use serde_json::{json, Map, Value};

use crate::model::{ConfigError, DefaultFolders, ItemType, LauncherConfig, LauncherItem, CURRENT_SCHEMA_VERSION};
use crate::pyish::path_identity;
use crate::storage::{render_json, write_atomically};
use crate::url;

pub const PORTABLE_FORMAT: &str = "quickaccess-launcher-config";
pub const PORTABLE_FORMAT_VERSION: i64 = 1;

#[derive(Debug, PartialEq, Eq)]
pub enum TransferError {
    Invalid(String),
    Schema(ConfigError),
}

impl fmt::Display for TransferError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) => f.write_str(message),
            Self::Schema(error) => write!(f, "{error}"),
        }
    }
}

fn invalid(message: &str) -> TransferError {
    TransferError::Invalid(message.to_string())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportMode {
    Merge,
    Replace,
}

/// An import plan holding the detached config it would produce.
#[derive(Debug)]
pub struct ImportPreview {
    pub mode: ImportMode,
    pub added: usize,
    pub updated: usize,
    pub skipped: usize,
    pub invalid: usize,
    pub conflicts: usize,
    pub removed: usize,
    pub settings_changed: bool,
    pub result: LauncherConfig,
}

impl ImportPreview {
    pub fn changed(&self) -> bool {
        self.added > 0 || self.updated > 0 || self.removed > 0 || self.settings_changed
    }
}

/// Serialize deterministically as human-readable UTF-8 JSON text.
pub fn export_portable_config(config: &LauncherConfig) -> String {
    render_json(&json!({
        "format": PORTABLE_FORMAT,
        "format_version": PORTABLE_FORMAT_VERSION,
        "config_schema_version": CURRENT_SCHEMA_VERSION,
        "config": config.to_value(),
    }))
}

/// Atomically write an export to an explicitly selected destination.
pub fn write_portable_config(destination: &Path, config: &LauncherConfig) -> std::io::Result<()> {
    if destination.file_name().is_none() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Export destination must be a file path",
        ));
    }
    write_atomically(destination, &export_portable_config(config), || {})
}

fn positive_version(value: Option<&Value>, field: &str) -> Result<i64, TransferError> {
    value
        .filter(|value| value.is_i64() || value.is_u64())
        .and_then(Value::as_i64)
        .filter(|version| *version >= 1)
        .ok_or_else(|| TransferError::Invalid(format!("'{field}' must be a positive integer")))
}

fn parse_document(source: &[u8]) -> Result<(Map<String, Value>, Vec<Value>), TransferError> {
    let document: Value = serde_json::from_slice(source)
        .map_err(|_| invalid("Portable configuration is not valid JSON"))?;
    let Value::Object(document) = document else {
        return Err(invalid("Portable configuration root must be a JSON object"));
    };
    if document.get("format").and_then(Value::as_str) != Some(PORTABLE_FORMAT) {
        return Err(invalid("Unrecognized portable configuration format"));
    }
    let format_version = positive_version(document.get("format_version"), "format_version")?;
    if format_version > PORTABLE_FORMAT_VERSION {
        return Err(TransferError::Invalid(format!(
            "Portable configuration format version {format_version} is newer than supported version {PORTABLE_FORMAT_VERSION}"
        )));
    }
    let too_new = |version| TransferError::Schema(ConfigError::UnsupportedSchema(version));
    let schema = positive_version(document.get("config_schema_version"), "config_schema_version")?;
    if schema > CURRENT_SCHEMA_VERSION {
        return Err(too_new(schema));
    }
    let Some(Value::Object(raw_config)) = document.get("config") else {
        return Err(invalid("'config' must be a JSON object"));
    };
    let embedded = positive_version(raw_config.get("schema_version"), "config.schema_version")?;
    if embedded > CURRENT_SCHEMA_VERSION {
        return Err(too_new(embedded));
    }
    if embedded != schema {
        return Err(invalid("Configuration schema metadata does not match"));
    }
    let Some(Value::Array(raw_items)) = raw_config.get("items") else {
        return Err(invalid("'config.items' must be a JSON array"));
    };
    Ok((raw_config.clone(), raw_items.clone()))
}

struct ParsedItem {
    item: LauncherItem,
    source_index: usize,
}

fn parse_item(raw: &Value) -> Result<LauncherItem, ConfigError> {
    let bad = |message: &str| ConfigError::Invalid(message.to_string());
    let object = raw.as_object().ok_or_else(|| bad("Item must be a JSON object"))?;
    let text = |key: &str| {
        object
            .get(key)
            .and_then(Value::as_str)
            .filter(|value| !crate::pyish::strip(value).is_empty())
    };
    let id = text("id").ok_or_else(|| bad("Item is missing a stable id"))?;
    let name = text("name").ok_or_else(|| bad("Item is missing a name"))?;
    let path = text("path").ok_or_else(|| bad("Item is missing a path"))?;
    let item_type = object
        .get("type")
        .and_then(Value::as_str)
        .and_then(ItemType::parse)
        .ok_or_else(|| bad("Item type must be folder, file, or url"))?;
    let order = object
        .get("order")
        .filter(|value| value.is_i64() || value.is_u64())
        .and_then(Value::as_i64)
        .filter(|order| *order >= 0)
        .ok_or_else(|| bad("Item order must be a non-negative integer"))?;
    // A known type means text is validated only; no path is ever touched.
    LauncherItem::new(id, name, path, item_type, order)
}

fn target_key(item: &LauncherItem) -> (bool, String) {
    if item.item_type == ItemType::Url {
        if let Ok(mut parts) = url::split(&item.path) {
            parts.netloc = caseless::default_case_fold_str(&parts.netloc);
            return (true, parts.unsplit());
        }
    }
    (false, path_identity(&item.path))
}

fn same_content(left: &LauncherItem, right: &LauncherItem) -> bool {
    left.id == right.id
        && left.name == right.name
        && left.item_type == right.item_type
        && left.path == right.path
}

fn settings_equal(left: &LauncherConfig, right: &LauncherConfig) -> bool {
    left.hotkey == right.hotkey
        && left.quick_add_hotkey == right.quick_add_hotkey
        && left.appearance_mode == right.appearance_mode
        && left.run_on_startup == right.run_on_startup
        && left.columns == right.columns
        && left.welcome_shown == right.welcome_shown
        && left.check_updates == right.check_updates
        && left.last_update_notice == right.last_update_notice
}

fn renumbered(settings: &LauncherConfig, items: Vec<LauncherItem>) -> Result<LauncherConfig, TransferError> {
    let mut result = settings.clone();
    result.items = items;
    for (index, item) in result.items.iter_mut().enumerate() {
        item.order = index as i64;
    }
    result.normalize().map_err(TransferError::Schema)?;
    Ok(result)
}

#[derive(Default)]
struct Counts {
    added: usize,
    updated: usize,
    skipped: usize,
    conflicts: usize,
}

fn preview_replace(
    current: &LauncherConfig,
    incoming: &LauncherConfig,
    parsed: &[ParsedItem],
) -> Result<(LauncherConfig, Counts, usize), TransferError> {
    let mut counts = Counts::default();
    let current_positions: HashMap<&str, usize> = current
        .items
        .iter()
        .enumerate()
        .map(|(index, item)| (item.id.as_str(), index))
        .collect();
    let mut accepted: Vec<LauncherItem> = Vec::new();
    let mut seen_ids: HashSet<String> = HashSet::new();
    let mut seen_targets: HashMap<(bool, String), String> = HashMap::new();
    for entry in parsed {
        let item = &entry.item;
        if seen_ids.contains(&item.id) {
            counts.conflicts += 1;
            continue;
        }
        let target = target_key(item);
        if seen_targets.get(&target).is_some_and(|owner| *owner != item.id) {
            counts.conflicts += 1;
            continue;
        }
        seen_ids.insert(item.id.clone());
        seen_targets.insert(target, item.id.clone());
        let new_position = accepted.len();
        accepted.push(item.clone());
        match current_positions.get(item.id.as_str()) {
            None => counts.added += 1,
            Some(position) => {
                if same_content(&current.items[*position], item) && *position == new_position {
                    counts.skipped += 1;
                } else {
                    counts.updated += 1;
                }
            }
        }
    }
    let removed = current.items.iter().filter(|item| !seen_ids.contains(&item.id)).count();
    Ok((renumbered(incoming, accepted)?, counts, removed))
}

fn preview_merge(
    current: &LauncherConfig,
    parsed: &[ParsedItem],
) -> Result<(LauncherConfig, Counts, usize), TransferError> {
    let mut counts = Counts::default();
    let mut items = current.items.clone();
    let mut owners: HashMap<(bool, String), HashSet<String>> = HashMap::new();
    for item in &items {
        owners.entry(target_key(item)).or_default().insert(item.id.clone());
    }
    let mut seen_ids: HashSet<String> = HashSet::new();
    for entry in parsed {
        let item = &entry.item;
        if !seen_ids.insert(item.id.clone()) {
            counts.conflicts += 1;
            continue;
        }
        let target = target_key(item);
        let target_owners = owners.get(&target).cloned().unwrap_or_default();
        if let Some(position) = items.iter().position(|existing| existing.id == item.id) {
            if target_owners.iter().any(|owner| *owner != item.id) {
                counts.conflicts += 1;
                continue;
            }
            if same_content(&items[position], item) {
                counts.skipped += 1;
                continue;
            }
            let old_target = target_key(&items[position]);
            if let Some(old) = owners.get_mut(&old_target) {
                old.remove(&item.id);
                if old.is_empty() {
                    owners.remove(&old_target);
                }
            }
            let order = items[position].order;
            items[position] = LauncherItem { order, ..item.clone() };
            owners.entry(target).or_default().insert(item.id.clone());
            counts.updated += 1;
            continue;
        }
        if !target_owners.is_empty() {
            counts.conflicts += 1;
            continue;
        }
        items.push(LauncherItem { order: items.len() as i64, ..item.clone() });
        owners.entry(target).or_default().insert(item.id.clone());
        counts.added += 1;
    }
    Ok((renumbered(current, items)?, counts, 0))
}

/// Plan an import without mutating `current` or touching any target path.
pub fn preview_config_import(
    current: &LauncherConfig,
    source: &[u8],
    mode: ImportMode,
) -> Result<ImportPreview, TransferError> {
    let (raw_config, raw_items) = parse_document(source)?;

    // Items are parsed separately below, so settings parse against no items.
    let mut settings_only = raw_config;
    settings_only.insert("items".to_string(), json!([]));
    let unused = DefaultFolders::under(".");
    let incoming = LauncherConfig::from_value(&Value::Object(settings_only), &unused)
        .map_err(TransferError::Schema)?;

    let mut parsed: Vec<ParsedItem> = Vec::new();
    let mut invalid_count = 0;
    for (source_index, raw) in raw_items.iter().enumerate() {
        match parse_item(raw) {
            Ok(item) => parsed.push(ParsedItem { item, source_index }),
            Err(_) => invalid_count += 1,
        }
    }
    parsed.sort_by_key(|entry| (entry.item.order, entry.source_index));

    let (result, counts, removed, settings_changed) = match mode {
        ImportMode::Replace => {
            let (result, counts, removed) = preview_replace(current, &incoming, &parsed)?;
            (result, counts, removed, !settings_equal(current, &incoming))
        }
        ImportMode::Merge => {
            let (result, counts, removed) = preview_merge(current, &parsed)?;
            (result, counts, removed, false)
        }
    };
    Ok(ImportPreview {
        mode,
        added: counts.added,
        updated: counts.updated,
        skipped: counts.skipped,
        invalid: invalid_count,
        conflicts: counts.conflicts,
        removed,
        settings_changed,
        result,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> LauncherConfig {
        let mut config = LauncherConfig::empty();
        config.items = vec![
            LauncherItem::new("a", "A", r"C:\a", ItemType::Folder, 0).unwrap(),
            LauncherItem::new("b", "B", "https://Example.com/x", ItemType::Url, 1).unwrap(),
        ];
        config
    }

    fn document(items: Value, settings: Value) -> Vec<u8> {
        let mut config = json!({"schema_version": 2, "items": items});
        for (key, value) in settings.as_object().unwrap() {
            config[key] = value.clone();
        }
        json!({
            "format": PORTABLE_FORMAT,
            "format_version": 1,
            "config_schema_version": 2,
            "config": config,
        })
        .to_string()
        .into_bytes()
    }

    fn entry(id: &str, name: &str, path: &str, kind: &str, order: i64) -> Value {
        json!({"id": id, "name": name, "path": path, "type": kind, "order": order})
    }

    #[test]
    fn export_round_trips_through_import() {
        let config = base();
        let text = export_portable_config(&config);
        assert!(text.ends_with('\n'));
        let preview = preview_config_import(&config, text.as_bytes(), ImportMode::Replace).unwrap();
        assert!(!preview.changed());
        assert_eq!(preview.skipped, 2);
        assert_eq!(preview.result, config);
    }

    #[test]
    fn rejects_foreign_or_future_documents() {
        let config = base();
        let attempt = |bytes: &[u8]| preview_config_import(&config, bytes, ImportMode::Merge);
        assert!(attempt(b"not json").is_err());
        assert!(attempt(b"[]").is_err());
        assert!(attempt(br#"{"format":"other"}"#).is_err());

        let mut doc: Value = serde_json::from_slice(&document(json!([]), json!({}))).unwrap();
        doc["format_version"] = json!(2);
        assert!(attempt(doc.to_string().as_bytes()).is_err());

        let mut doc: Value = serde_json::from_slice(&document(json!([]), json!({}))).unwrap();
        doc["config_schema_version"] = json!(9);
        assert!(matches!(
            attempt(doc.to_string().as_bytes()),
            Err(TransferError::Schema(ConfigError::UnsupportedSchema(9)))
        ));

        let mut doc: Value = serde_json::from_slice(&document(json!([]), json!({}))).unwrap();
        doc["config_schema_version"] = json!(1);
        assert!(attempt(doc.to_string().as_bytes()).is_err());
    }

    #[test]
    fn merge_adds_updates_and_keeps_settings() {
        let config = base();
        let source = document(
            json!([
                entry("a", "새 이름", r"C:\a", "folder", 0),
                entry("c", "C", r"C:\c", "file", 1),
                entry("b", "B", "https://Example.com/x", "url", 2),
            ]),
            json!({"hotkey": "ctrl+k"}),
        );
        let preview = preview_config_import(&config, &source, ImportMode::Merge).unwrap();
        assert_eq!((preview.added, preview.updated, preview.skipped), (1, 1, 1));
        assert!(!preview.settings_changed);
        assert_eq!(preview.result.hotkey, config.hotkey);
        assert_eq!(preview.result.items.len(), 3);
        assert_eq!(preview.result.items[0].name, "새 이름");
        assert_eq!(preview.result.items[2].id, "c");
    }

    #[test]
    fn merge_reports_target_and_id_conflicts() {
        let config = base();
        let source = document(
            json!([
                entry("x", "dup local", r"c:/A/", "folder", 0),
                entry("y", "dup url", "https://EXAMPLE.com/x", "url", 1),
                entry("a", "steal", "https://example.com/x", "url", 2),
                entry("z", "ok", r"C:\z", "file", 3),
                entry("z", "again", r"C:\zz", "file", 4),
            ]),
            json!({}),
        );
        let preview = preview_config_import(&config, &source, ImportMode::Merge).unwrap();
        assert_eq!(preview.conflicts, 4);
        assert_eq!(preview.added, 1);
        assert_eq!(preview.result.items.len(), 3);
    }

    #[test]
    fn invalid_items_are_counted_not_fatal() {
        let source = document(
            json!([
                7,
                {"id":"x","name":"n","path":"C:\\x","type":"bad","order":0},
                {"id":"x","name":"n","path":"C:\\x","type":"file","order":-1},
                {"id":"x","name":"n","path":"C:\\x","type":"file","order":true},
                {"id":" ","name":"n","path":"C:\\x","type":"file","order":0},
                {"id":"u","name":"n","path":"ftp://x","type":"url","order":0},
                entry("ok", "ok", r"C:\ok", "file", 0),
            ]),
            json!({}),
        );
        let preview = preview_config_import(&base(), &source, ImportMode::Merge).unwrap();
        assert_eq!(preview.invalid, 6);
        assert_eq!(preview.added, 1);
    }

    #[test]
    fn replace_applies_settings_and_removes_items() {
        let config = base();
        let source = document(
            json!([
                entry("c", "C", r"C:\c", "file", 5),
                entry("a", "A", r"C:\a", "folder", 1),
                entry("d", "dup", r"C:\C", "file", 9),
            ]),
            json!({"hotkey": "ctrl+k", "columns": 4}),
        );
        let preview = preview_config_import(&config, &source, ImportMode::Replace).unwrap();
        assert!(preview.settings_changed);
        assert_eq!(preview.result.hotkey, "ctrl+k");
        assert_eq!(preview.result.columns, 4);
        assert_eq!(preview.removed, 1);
        assert_eq!(preview.conflicts, 1);
        assert_eq!(preview.skipped, 1);
        assert_eq!(preview.added, 1);
        let ids: Vec<&str> = preview.result.items.iter().map(|i| i.id.as_str()).collect();
        assert_eq!(ids, ["a", "c"]);
    }

    #[test]
    fn replace_treats_a_moved_item_as_updated() {
        let config = base();
        let source = document(
            json!([
                entry("b", "B", "https://Example.com/x", "url", 0),
                entry("a", "A", r"C:\a", "folder", 1),
            ]),
            json!({}),
        );
        let preview = preview_config_import(&config, &source, ImportMode::Replace).unwrap();
        assert_eq!(preview.updated, 2);
        assert_eq!(preview.skipped, 0);
    }

    #[test]
    fn preview_leaves_the_current_config_unchanged() {
        let config = base();
        let snapshot = config.clone();
        let source = document(json!([entry("c", "C", r"C:\c", "file", 0)]), json!({}));
        preview_config_import(&config, &source, ImportMode::Replace).unwrap();
        assert_eq!(config, snapshot);
    }

    #[test]
    fn writes_exports_atomically() {
        let directory = std::env::temp_dir().join(format!("qa-export-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("backup.json");
        write_portable_config(&path, &base()).unwrap();
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["format"], PORTABLE_FORMAT);
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 1);
        std::fs::remove_dir_all(&directory).unwrap();
    }
}
