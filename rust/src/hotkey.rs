//! Shortcut parsing and transactional replacement of native registrations.

use std::fmt;

pub const MOD_ALT: u32 = 0x0001;
pub const MOD_CONTROL: u32 = 0x0002;
pub const MOD_SHIFT: u32 = 0x0004;
pub const MOD_WIN: u32 = 0x0008;

const BASE_HOTKEY_ID: i32 = 0x5100;
const MODIFIER_ORDER: [(&str, u32); 4] = [
    ("ctrl", MOD_CONTROL),
    ("alt", MOD_ALT),
    ("shift", MOD_SHIFT),
    ("win", MOD_WIN),
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedHotkey {
    pub modifiers: u32,
    pub virtual_key: u32,
    pub canonical: String,
}

fn modifier(token: &str) -> Option<(&'static str, u32)> {
    match token {
        "ctrl" | "control" => Some(("ctrl", MOD_CONTROL)),
        "alt" | "option" => Some(("alt", MOD_ALT)),
        "shift" => Some(("shift", MOD_SHIFT)),
        "win" | "windows" | "super" => Some(("win", MOD_WIN)),
        _ => None,
    }
}

fn named_key(token: &str) -> Option<(&'static str, u32)> {
    Some(match token {
        "backspace" => ("backspace", 0x08),
        "tab" => ("tab", 0x09),
        "enter" | "return" => ("enter", 0x0D),
        "pause" => ("pause", 0x13),
        "capslock" | "caps lock" => ("capslock", 0x14),
        "esc" | "escape" => ("esc", 0x1B),
        "space" => ("space", 0x20),
        "pageup" | "page up" => ("pageup", 0x21),
        "pagedown" | "page down" => ("pagedown", 0x22),
        "end" => ("end", 0x23),
        "home" => ("home", 0x24),
        "left" => ("left", 0x25),
        "up" => ("up", 0x26),
        "right" => ("right", 0x27),
        "down" => ("down", 0x28),
        "printscreen" | "print screen" => ("printscreen", 0x2C),
        "insert" => ("insert", 0x2D),
        "delete" | "del" => ("delete", 0x2E),
        ";" => (";", 0xBA),
        "=" => ("=", 0xBB),
        "," => (",", 0xBC),
        "-" => ("-", 0xBD),
        "." => (".", 0xBE),
        "/" => ("/", 0xBF),
        "`" => ("`", 0xC0),
        "[" => ("[", 0xDB),
        "\\" => ("\\", 0xDC),
        "]" => ("]", 0xDD),
        "'" => ("'", 0xDE),
        _ => return None,
    })
}

fn parse_key(token: &str) -> Result<(String, u32), String> {
    if let Some((name, code)) = named_key(token) {
        return Ok((name.to_string(), code));
    }
    if token.len() == 1 {
        let byte = token.as_bytes()[0];
        if byte.is_ascii_lowercase() || byte.is_ascii_digit() {
            return Ok((token.to_string(), u32::from(byte.to_ascii_uppercase())));
        }
    }
    if let Some(number) = token.strip_prefix('f').and_then(|rest| rest.parse::<u32>().ok()) {
        if (1..=24).contains(&number) {
            return Ok((format!("f{number}"), 0x70 + number - 1));
        }
    }
    if let Some(number) = token.strip_prefix("numpad").and_then(|rest| rest.parse::<u32>().ok()) {
        if number <= 9 {
            return Ok((format!("numpad{number}"), 0x60 + number));
        }
    }
    Err(format!("unsupported hotkey key: {token:?}"))
}

/// Parse a user-facing shortcut such as `ctrl+shift+space`.
pub fn parse_hotkey(shortcut: &str) -> Result<ParsedHotkey, String> {
    if shortcut.trim().is_empty() {
        return Err("hotkey must be a non-empty string".to_string());
    }
    let mut modifiers = 0_u32;
    let mut key: Option<(String, u32)> = None;
    for raw in shortcut.split('+') {
        let token = raw.trim().to_lowercase();
        if token.is_empty() {
            return Err("hotkey contains an empty component".to_string());
        }
        if let Some((name, flag)) = modifier(&token) {
            if modifiers & flag != 0 {
                return Err(format!("duplicate modifier: {name}"));
            }
            modifiers |= flag;
            continue;
        }
        if key.is_some() {
            return Err("hotkey must contain exactly one non-modifier key".to_string());
        }
        key = Some(parse_key(&token)?);
    }
    let Some((key_name, virtual_key)) = key else {
        return Err("hotkey must contain a non-modifier key".to_string());
    };
    if modifiers == 0 {
        return Err("a global hotkey must contain at least one modifier".to_string());
    }
    let mut parts: Vec<&str> = MODIFIER_ORDER
        .iter()
        .filter(|(_, flag)| modifiers & flag != 0)
        .map(|(name, _)| *name)
        .collect();
    parts.push(&key_name);
    Ok(ParsedHotkey {
        modifiers,
        virtual_key,
        canonical: parts.join("+"),
    })
}

/// A static caution for shortcuts that silently coexist with other software.
///
/// `RegisterHotKey` only fails for a combination another app also registered,
/// so IME toggles and IDE conventions cannot be detected at runtime.
pub fn describe_conflict_risk(shortcut: &str) -> Option<&'static str> {
    match parse_hotkey(shortcut).ok()?.canonical.as_str() {
        "ctrl+space" => Some("Windows 한/영 전환 및 일부 IDE(Ctrl+Space 자동완성)와 겹칠 수 있습니다."),
        "ctrl+shift+space" => Some("일부 IDE의 매개변수 정보 단축키와 겹칠 수 있습니다."),
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Binding {
    Panel,
    QuickAdd,
}

#[derive(Debug, PartialEq, Eq)]
pub enum HotkeyError {
    Parse(String),
    Registration {
        message: String,
        rollback_succeeded: bool,
    },
}

impl fmt::Display for HotkeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Parse(message) => f.write_str(message),
            Self::Registration { message, .. } => f.write_str(message),
        }
    }
}

/// The native surface, injectable so rollback can be tested deterministically.
pub trait HotkeyApi {
    fn register(&mut self, id: i32, hotkey: &ParsedHotkey) -> Result<(), String>;
    fn unregister(&mut self, id: i32) -> Result<(), String>;
}

#[derive(Debug, Clone)]
struct Registration {
    id: i32,
    binding: Binding,
    hotkey: ParsedHotkey,
}

/// The complete registration set, replaced transactionally.
#[derive(Default)]
pub struct HotkeyManager {
    registrations: Vec<Registration>,
}

impl HotkeyManager {
    pub fn get(&self, binding: Binding) -> Option<&str> {
        self.registrations
            .iter()
            .find(|registration| registration.binding == binding)
            .map(|registration| registration.hotkey.canonical.as_str())
    }

    pub fn binding_for(&self, id: i32) -> Option<Binding> {
        self.registrations
            .iter()
            .find(|registration| registration.id == id)
            .map(|registration| registration.binding)
    }

    pub fn is_complete(&self) -> bool {
        self.get(Binding::Panel).is_some() && self.get(Binding::QuickAdd).is_some()
    }

    /// Replace both bindings, restoring the old set when anything fails.
    pub fn configure(
        &mut self,
        api: &mut dyn HotkeyApi,
        panel: &str,
        quick_add: &str,
    ) -> Result<(), HotkeyError> {
        let panel = parse_hotkey(panel).map_err(HotkeyError::Parse)?;
        let quick_add = parse_hotkey(quick_add).map_err(HotkeyError::Parse)?;
        if (panel.modifiers, panel.virtual_key) == (quick_add.modifiers, quick_add.virtual_key) {
            return Err(HotkeyError::Parse(format!(
                "'quick_add' duplicates 'panel': {}",
                panel.canonical
            )));
        }
        let desired = [(Binding::Panel, panel), (Binding::QuickAdd, quick_add)];
        let same_layout = self.registrations.len() == desired.len()
            && self.registrations.iter().zip(&desired).all(|(current, (binding, hotkey))| {
                current.binding == *binding
                    && current.hotkey.modifiers == hotkey.modifiers
                    && current.hotkey.virtual_key == hotkey.virtual_key
            });
        if same_layout {
            return Ok(());
        }

        let old = std::mem::take(&mut self.registrations);
        let mut removed: Vec<Registration> = Vec::new();
        for registration in &old {
            if let Err(error) = api.unregister(registration.id) {
                let rollback = register_all(api, &removed);
                self.registrations = if rollback {
                    old.clone()
                } else {
                    let ids: Vec<i32> = removed.iter().map(|r| r.id).collect();
                    old.iter().filter(|r| !ids.contains(&r.id)).cloned().collect()
                };
                return Err(HotkeyError::Registration {
                    message: format!("could not replace existing hotkeys: {error}"),
                    rollback_succeeded: rollback,
                });
            }
            removed.push(registration.clone());
        }

        let mut registered: Vec<Registration> = Vec::new();
        for (index, (binding, hotkey)) in desired.iter().enumerate() {
            let registration = Registration {
                id: BASE_HOTKEY_ID + index as i32,
                binding: *binding,
                hotkey: hotkey.clone(),
            };
            if let Err(error) = api.register(registration.id, hotkey) {
                let cleanup = unregister_all(api, &registered);
                let rollback = register_all(api, &old);
                if rollback {
                    self.registrations = old;
                }
                return Err(HotkeyError::Registration {
                    message: format!("could not register new hotkeys: {error}"),
                    rollback_succeeded: cleanup && rollback,
                });
            }
            registered.push(registration);
        }
        self.registrations = registered;
        Ok(())
    }

    pub fn clear(&mut self, api: &mut dyn HotkeyApi) {
        for registration in std::mem::take(&mut self.registrations) {
            let _ = api.unregister(registration.id);
        }
    }
}

fn register_all(api: &mut dyn HotkeyApi, registrations: &[Registration]) -> bool {
    let mut successful: Vec<Registration> = Vec::new();
    for registration in registrations {
        if api.register(registration.id, &registration.hotkey).is_err() {
            unregister_all(api, &successful);
            return false;
        }
        successful.push(registration.clone());
    }
    true
}

fn unregister_all(api: &mut dyn HotkeyApi, registrations: &[Registration]) -> bool {
    let mut succeeded = true;
    for registration in registrations.iter().rev() {
        succeeded &= api.unregister(registration.id).is_ok();
    }
    succeeded
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    #[test]
    fn parses_and_canonicalizes() {
        let parsed = parse_hotkey("Shift + CONTROL+Space").unwrap();
        assert_eq!(parsed.canonical, "ctrl+shift+space");
        assert_eq!(parsed.modifiers, MOD_CONTROL | MOD_SHIFT);
        assert_eq!(parsed.virtual_key, 0x20);
        assert_eq!(parse_hotkey("alt+a").unwrap().virtual_key, 0x41);
        assert_eq!(parse_hotkey("win+f24").unwrap().virtual_key, 0x87);
        assert_eq!(parse_hotkey("ctrl+numpad7").unwrap().virtual_key, 0x67);
        assert_eq!(parse_hotkey("super+;").unwrap().canonical, "win+;");
        assert_eq!(parse_hotkey("ctrl+page up").unwrap().canonical, "ctrl+pageup");
    }

    #[test]
    fn rejects_invalid_shortcuts() {
        for bad in [
            "", "  ", "space", "ctrl", "ctrl+", "ctrl+ctrl+a", "ctrl+a+b", "ctrl+f25", "ctrl+f0",
            "ctrl+numpad10", "ctrl+é",
        ] {
            assert!(parse_hotkey(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn warns_only_for_known_conflicts() {
        assert!(describe_conflict_risk("Space+Ctrl").is_some());
        assert!(describe_conflict_risk("ctrl+shift+space").is_some());
        assert!(describe_conflict_risk("ctrl+alt+k").is_none());
        assert!(describe_conflict_risk("nonsense").is_none());
    }

    #[derive(Default)]
    struct FakeApi {
        active: HashMap<i32, String>,
        blocked: HashSet<String>,
        fail_unregister: HashSet<i32>,
        log: Vec<String>,
    }

    impl HotkeyApi for FakeApi {
        fn register(&mut self, id: i32, hotkey: &ParsedHotkey) -> Result<(), String> {
            self.log.push(format!("+{}", hotkey.canonical));
            if self.blocked.contains(&hotkey.canonical) {
                return Err("in use".to_string());
            }
            self.active.insert(id, hotkey.canonical.clone());
            Ok(())
        }

        fn unregister(&mut self, id: i32) -> Result<(), String> {
            if self.fail_unregister.contains(&id) {
                return Err("denied".to_string());
            }
            self.active.remove(&id);
            Ok(())
        }
    }

    fn active(api: &FakeApi) -> Vec<String> {
        let mut values: Vec<String> = api.active.values().cloned().collect();
        values.sort();
        values
    }

    #[test]
    fn registers_a_complete_set() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        assert!(manager.is_complete());
        assert_eq!(manager.get(Binding::Panel), Some("ctrl+k"));
        assert_eq!(manager.binding_for(BASE_HOTKEY_ID + 1), Some(Binding::QuickAdd));
        assert_eq!(manager.binding_for(1), None);
    }

    #[test]
    fn unchanged_layout_touches_nothing() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        api.log.clear();
        manager.configure(&mut api, "control+K", "ctrl+j").unwrap();
        assert!(api.log.is_empty());
    }

    #[test]
    fn invalid_input_changes_nothing() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        assert!(matches!(
            manager.configure(&mut api, "bad", "ctrl+j"),
            Err(HotkeyError::Parse(_))
        ));
        assert!(matches!(
            manager.configure(&mut api, "ctrl+x", "control+x"),
            Err(HotkeyError::Parse(_))
        ));
        assert_eq!(active(&api), ["ctrl+j", "ctrl+k"]);
    }

    #[test]
    fn failed_registration_restores_the_old_set() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        api.blocked.insert("ctrl+y".to_string());
        let error = manager.configure(&mut api, "ctrl+x", "ctrl+y").unwrap_err();
        assert!(matches!(
            error,
            HotkeyError::Registration { rollback_succeeded: true, .. }
        ));
        assert_eq!(active(&api), ["ctrl+j", "ctrl+k"]);
        assert_eq!(manager.get(Binding::Panel), Some("ctrl+k"));
    }

    #[test]
    fn reports_a_failed_rollback() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        api.blocked.insert("ctrl+y".to_string());
        api.blocked.insert("ctrl+k".to_string());
        let error = manager.configure(&mut api, "ctrl+x", "ctrl+y").unwrap_err();
        assert!(matches!(
            error,
            HotkeyError::Registration { rollback_succeeded: false, .. }
        ));
        assert!(!manager.is_complete());
    }

    #[test]
    fn failed_unregister_keeps_the_active_set() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        api.fail_unregister.insert(BASE_HOTKEY_ID + 1);
        assert!(manager.configure(&mut api, "ctrl+x", "ctrl+y").is_err());
        assert_eq!(active(&api), ["ctrl+j", "ctrl+k"]);
        assert_eq!(manager.get(Binding::QuickAdd), Some("ctrl+j"));
    }

    #[test]
    fn clear_unregisters_everything() {
        let mut api = FakeApi::default();
        let mut manager = HotkeyManager::default();
        manager.configure(&mut api, "ctrl+k", "ctrl+j").unwrap();
        manager.clear(&mut api);
        assert!(active(&api).is_empty());
        assert!(!manager.is_complete());
    }
}
