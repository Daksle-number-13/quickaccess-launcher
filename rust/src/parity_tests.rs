//! Compare every pure rule with answers recorded from the Python release.
//! Regenerate the data with `python devtools/generate_rust_parity.py`.

use serde_json::{json, Value};

use crate::geometry::{clamp_to_work_area, popup_dimensions, Point, Rect, Size};
use crate::hotkey::parse_hotkey;
use crate::icons::icon_key;
use crate::model::{normalize_web_url, DefaultFolders, ItemType, LauncherConfig, LauncherItem};
use crate::pyish::{basename, extension, normpath, split_root};
use crate::search::SearchIndex;
use crate::startup_rules::{command_matches, format_command, split_command_line};

fn data() -> Value {
    serde_json::from_str(include_str!("../tests-data/python_parity.json")).unwrap()
}

fn list<'a>(document: &'a Value, key: &str) -> &'a Vec<Value> {
    document[key].as_array().unwrap()
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}

fn folders() -> DefaultFolders {
    DefaultFolders::under(r"C:\Users\tester")
}

/// Generated ids are random in both implementations, so only compare given ids.
fn mask_generated_ids(input: &Value, mut value: Value) -> Value {
    let raw = input.get("items").and_then(Value::as_array).cloned().unwrap_or_default();
    let known: Vec<&str> = raw.iter().filter_map(|item| item["id"].as_str()).collect();
    if let Some(items) = value["items"].as_array_mut() {
        for item in items {
            if !known.contains(&item["id"].as_str().unwrap_or("")) {
                item["id"] = json!("<generated>");
            }
        }
    }
    value
}

#[test]
fn configs_match() {
    let document = data();
    for case in list(&document, "configs") {
        let parsed = LauncherConfig::from_value(&case["input"], &folders()).unwrap();
        assert_eq!(
            mask_generated_ids(&case["input"], parsed.to_value()),
            mask_generated_ids(&case["input"], case["answer"].clone()),
            "input: {}",
            case["input"]
        );
    }
    // The saved file must be byte-identical to what Python writes.
    for case in list(&document, "configs") {
        assert_eq!(crate::storage::render_json(&case["answer"]), text(&case["rendered"]));
    }
    for case in list(&document, "invalid_configs") {
        assert!(LauncherConfig::from_value(case, &folders()).is_err(), "{case}");
    }
}

#[test]
fn urls_match() {
    let document = data();
    for case in list(&document, "urls") {
        let input = text(&case["input"]);
        match normalize_web_url(input) {
            Ok(url) => assert_eq!(url, case["answer"]["ok"].as_str().unwrap_or("<python rejected>"), "{input}"),
            Err(_) => assert!(case["answer"].get("error").is_some(), "{input} => {}", case["answer"]),
        }
    }
}

#[test]
fn searches_match() {
    let document = data();
    let items: Vec<LauncherItem> = list(&document, "items")
        .iter()
        .enumerate()
        .map(|(index, item)| {
            LauncherItem::new(
                text(&item["id"]),
                text(&item["name"]),
                text(&item["path"]),
                ItemType::parse(text(&item["type"])).unwrap(),
                index as i64,
            )
            .unwrap()
        })
        .collect();
    let index = SearchIndex::new(&items);
    for case in list(&document, "searches") {
        let answer: Vec<&str> = case["answer"].as_array().unwrap().iter().map(text).collect();
        let found: Vec<&str> = index
            .search(text(&case["query"]))
            .into_iter()
            .map(|position| items[position].id.as_str())
            .collect();
        assert_eq!(found, answer, "query: {}", case["query"]);
    }
}

#[test]
fn hotkeys_match() {
    let document = data();
    for case in list(&document, "hotkeys") {
        let input = text(&case["input"]);
        match parse_hotkey(input) {
            Ok(parsed) => {
                let ok = &case["answer"]["ok"];
                assert!(ok.is_object(), "python rejected {input}");
                assert_eq!(parsed.canonical, text(&ok["canonical"]), "{input}");
                assert_eq!(i64::from(parsed.modifiers), ok["modifiers"].as_i64().unwrap(), "{input}");
                assert_eq!(i64::from(parsed.virtual_key), ok["virtual_key"].as_i64().unwrap(), "{input}");
            }
            Err(_) => assert!(case["answer"].get("error").is_some(), "{input} => {}", case["answer"]),
        }
    }
}

#[test]
fn windows_paths_match() {
    let document = data();
    for case in list(&document, "paths") {
        let input = text(&case["input"]);
        assert_eq!(normpath(input), text(&case["normpath"]), "normpath {input}");
        assert_eq!(basename(input), text(&case["basename"]), "basename {input}");
        assert_eq!(extension(input), text(&case["extension"]), "extension {input}");
        let root = split_root(input);
        let answer = case["splitroot"].as_array().unwrap();
        assert_eq!(
            [root.0, root.1, root.2],
            [text(&answer[0]), text(&answer[1]), text(&answer[2])],
            "splitroot {input}"
        );
    }
    for case in list(&document, "icon_keys") {
        let input = text(&case["input"]);
        assert_eq!(icon_key(input, ItemType::File), text(&case["answer"]), "{input}");
    }
}

#[test]
fn startup_commands_match() {
    let document = data();
    let no_env = |_: &str| None;
    for case in list(&document, "commands") {
        let parts: Vec<&str> = case["input"].as_array().unwrap().iter().map(text).collect();
        assert_eq!(format_command(parts[0], &parts[1..]), text(&case["answer"]), "{parts:?}");
    }
    for case in list(&document, "command_lines") {
        let answer: Vec<&str> = case["answer"].as_array().unwrap().iter().map(text).collect();
        assert_eq!(split_command_line(text(&case["input"])), answer, "{}", case["input"]);
    }
    for case in list(&document, "command_matches") {
        let arguments: Vec<&str> = case["arguments"].as_array().unwrap().iter().map(text).collect();
        assert_eq!(
            command_matches(text(&case["actual"]), text(&case["executable"]), &arguments, &no_env),
            case["answer"].as_bool().unwrap(),
            "{}",
            case["actual"]
        );
    }
}

#[test]
fn versions_match() {
    let document = data();
    for case in list(&document, "versions") {
        let parsed = crate::update_rules::parse_version(text(&case["input"]));
        let answer = case["answer"].as_array().map(|parts| {
            (parts[0].as_u64().unwrap(), parts[1].as_u64().unwrap(), parts[2].as_u64().unwrap())
        });
        assert_eq!(parsed, answer, "{}", case["input"]);
    }
}

fn rect(value: &Value) -> Rect {
    let n = |index: usize| value[index].as_i64().unwrap() as i32;
    Rect { left: n(0), top: n(1), right: n(2), bottom: n(3) }
}

#[test]
fn geometry_matches() {
    let document = data();
    for case in list(&document, "clamps") {
        let n = |key: &str, index: usize| case[key][index].as_i64().unwrap() as i32;
        let point = clamp_to_work_area(
            Point { x: n("cursor", 0), y: n("cursor", 1) },
            Size { width: n("size", 0), height: n("size", 1) },
            rect(&case["rect"]),
        );
        assert_eq!([point.x, point.y], [n("answer", 0), n("answer", 1)], "{case}");
    }
    let cases = document["popup_dimensions"].as_array().expect("recorded with customtkinter installed");
    for case in cases {
        let layout = popup_dimensions(
            case["count"].as_i64().unwrap() as i32,
            case["columns"].as_i64().unwrap() as i32,
            rect(&case["rect"]),
            case["scale"].as_f64().unwrap(),
        );
        let answer: Vec<i64> = case["answer"].as_array().unwrap().iter().map(|value| value.as_i64().unwrap()).collect();
        assert_eq!(
            vec![
                i64::from(layout.width),
                i64::from(layout.height),
                i64::from(layout.columns),
                i64::from(layout.viewport_height)
            ],
            answer,
            "{case}"
        );
    }
}
