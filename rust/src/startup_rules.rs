//! Pure rules for the per-user Run command: building, parsing and comparison.

use std::fmt;

use crate::pyish::{normcase, normpath};

pub const MAX_RUN_COMMAND_LENGTH: usize = 260;

#[derive(Debug, PartialEq, Eq)]
pub struct CommandTooLong {
    pub actual_length: usize,
}

impl fmt::Display for CommandTooLong {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Windows 시작프로그램 명령은 {MAX_RUN_COMMAND_LENGTH}자를 초과할 수 없습니다 (현재 {}자). QuickAccess.exe를 더 짧은 폴더 경로로 이동해 주세요.",
            self.actual_length
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RegistrationState {
    Absent,
    Correct,
    Stale,
    Unreadable,
}

impl RegistrationState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Absent => "absent",
            Self::Correct => "correct",
            Self::Stale => "stale",
            Self::Unreadable => "unreadable",
        }
    }
}

/// A registry snapshot kept separate from the persisted user preference.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistrationStatus {
    pub state: RegistrationState,
    pub desired_enabled: bool,
    pub expected_command: String,
    pub actual_command: Option<String>,
    pub error: Option<String>,
}

impl RegistrationStatus {
    pub fn in_sync(&self) -> bool {
        match self.state {
            RegistrationState::Unreadable => false,
            RegistrationState::Correct => self.desired_enabled,
            RegistrationState::Absent => !self.desired_enabled,
            RegistrationState::Stale => false,
        }
    }
}

/// Expand Windows `%NAME%` variables case-insensitively and in memory.
pub fn expand_environment(value: &str, lookup: &dyn Fn(&str) -> Option<String>) -> String {
    let mut output = String::new();
    let mut rest = value;
    while let Some(open) = rest.find('%') {
        let after = &rest[open + 1..];
        let Some(close) = after.find('%') else { break };
        let name = &after[..close];
        if name.is_empty() {
            // `%%` never matches `%([^%]+)%`; keep the first `%` literal and
            // let the second begin the next possible variable.
            output.push_str(&rest[..=open]);
            rest = after;
            continue;
        }
        output.push_str(&rest[..open]);
        match lookup(name) {
            Some(replacement) => output.push_str(&replacement),
            None => output.push_str(&rest[open..open + close + 2]),
        }
        rest = &after[close + 1..];
    }
    output.push_str(rest);
    output
}

fn normalize_executable(value: &str, lookup: &dyn Fn(&str) -> Option<String>) -> String {
    let mut expanded = expand_environment(value, lookup);
    // The extended-length spelling is equivalent for a local absolute path;
    // normalizing it avoids a false stale result after installers rewrite it.
    let lowered = expanded.to_lowercase();
    if lowered.starts_with("\\\\?\\unc\\") {
        expanded = format!("\\\\{}", &expanded[8..]);
    } else if lowered.starts_with("\\\\?\\") {
        expanded = expanded[4..].to_string();
    }
    normcase(&normpath(&expanded))
}

/// Parse a Windows command line using `CommandLineToArgvW`-style rules.
pub fn split_command_line(command: &str) -> Vec<String> {
    let chars: Vec<char> = command.chars().collect();
    let length = chars.len();
    let is_ws = |c: char| c == ' ' || c == '\t';
    let mut parsed = Vec::new();
    let mut index = 0;
    loop {
        while index < length && is_ws(chars[index]) {
            index += 1;
        }
        if index >= length {
            return parsed;
        }
        let mut argument = String::new();
        let mut in_quotes = false;
        while index < length {
            let character = chars[index];
            if is_ws(character) && !in_quotes {
                break;
            }
            if character == '\\' {
                let start = index;
                while index < length && chars[index] == '\\' {
                    index += 1;
                }
                let slashes = index - start;
                if index < length && chars[index] == '"' {
                    argument.extend(std::iter::repeat('\\').take(slashes / 2));
                    if slashes % 2 == 1 {
                        argument.push('"');
                        index += 1;
                    } else if in_quotes && index + 1 < length && chars[index + 1] == '"' {
                        argument.push('"');
                        index += 2;
                    } else {
                        in_quotes = !in_quotes;
                        index += 1;
                    }
                } else {
                    argument.extend(std::iter::repeat('\\').take(slashes));
                }
                continue;
            }
            if character == '"' {
                if in_quotes && index + 1 < length && chars[index + 1] == '"' {
                    argument.push('"');
                    index += 2;
                } else {
                    in_quotes = !in_quotes;
                    index += 1;
                }
                continue;
            }
            argument.push(character);
            index += 1;
        }
        parsed.push(argument);
    }
}

/// Whether a Run value invokes exactly the expected startup entry.
///
/// Executables compare with Windows' case-insensitive path semantics; arguments
/// stay exact because changing an option can change behaviour.
pub fn command_matches(
    actual: &str,
    executable: &str,
    arguments: &[&str],
    lookup: &dyn Fn(&str) -> Option<String>,
) -> bool {
    if actual.trim().is_empty() {
        return false;
    }
    let parts = split_command_line(actual);
    if parts.len() != arguments.len() + 1 {
        return false;
    }
    if normalize_executable(&parts[0], lookup) != normalize_executable(executable, lookup) {
        return false;
    }
    parts[1..].iter().map(String::as_str).eq(arguments.iter().copied())
}

/// `subprocess.list2cmdline`
pub fn format_command(executable: &str, arguments: &[&str]) -> String {
    let mut output = String::new();
    for (position, argument) in std::iter::once(&executable).chain(arguments.iter()).enumerate() {
        if position > 0 {
            output.push(' ');
        }
        let quote = argument.is_empty() || argument.contains([' ', '\t']);
        if quote {
            output.push('"');
        }
        let mut slashes = 0;
        for character in argument.chars() {
            match character {
                '\\' => slashes += 1,
                '"' => {
                    output.extend(std::iter::repeat('\\').take(slashes * 2 + 1));
                    output.push('"');
                    slashes = 0;
                }
                other => {
                    output.extend(std::iter::repeat('\\').take(slashes));
                    slashes = 0;
                    output.push(other);
                }
            }
        }
        output.extend(std::iter::repeat('\\').take(if quote { slashes * 2 } else { slashes }));
        if quote {
            output.push('"');
        }
    }
    output
}

/// Windows measures command lines in UTF-16 code units, not characters.
pub fn validate_length(command: &str) -> Result<(), CommandTooLong> {
    let length = command.encode_utf16().count();
    if length > MAX_RUN_COMMAND_LENGTH {
        return Err(CommandTooLong { actual_length: length });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn env(name: &str) -> Option<String> {
        name.eq_ignore_ascii_case("localappdata")
            .then(|| r"C:\Users\me\AppData\Local".to_string())
    }

    #[test]
    fn formats_like_list2cmdline() {
        assert_eq!(format_command(r"C:\a.exe", &["--startup"]), r"C:\a.exe --startup");
        assert_eq!(
            format_command(r"C:\Program Files\a.exe", &["--startup"]),
            r#""C:\Program Files\a.exe" --startup"#
        );
        assert_eq!(format_command("a", &[""]), r#"a """#);
        assert_eq!(format_command("a", &[r#"x"y"#]), r#"a x\"y"#);
        assert_eq!(format_command("a", &[r"C:\my dir\"]), r#"a "C:\my dir\\""#);
    }

    #[test]
    fn splits_what_it_formats() {
        for arguments in [
            vec![r"C:\Program Files\한글\a.exe", "--startup"],
            vec![r"C:\a.exe", r#"x"y"#, r"C:\my dir\", ""],
        ] {
            let command = format_command(arguments[0], &arguments[1..]);
            assert_eq!(split_command_line(&command), arguments);
        }
        assert_eq!(split_command_line("  a   b\t"), ["a", "b"]);
        assert!(split_command_line("   ").is_empty());
        assert_eq!(split_command_line(r#""a""b" c"#), [r#"a"b"#, "c"]);
    }

    #[test]
    fn compares_equivalent_spellings() {
        let exe = r"C:\Users\Me\QuickAccess.exe";
        let args = ["--startup"];
        for actual in [
            r"C:\Users\Me\QuickAccess.exe --startup",
            r#""c:\users\me\quickaccess.exe" --startup"#,
            r"C:/Users/Me/./QuickAccess.exe --startup",
            r"\\?\C:\Users\Me\QuickAccess.exe --startup",
        ] {
            assert!(command_matches(actual, exe, &args, &no_env), "{actual}");
        }
        assert!(command_matches(
            r"\\?\UNC\srv\share\a.exe --startup",
            r"\\srv\share\a.exe",
            &args,
            &no_env
        ));
        assert!(command_matches(
            r"%LocalAppData%\a.exe --startup",
            r"C:\Users\me\AppData\Local\a.exe",
            &args,
            &env
        ));
    }

    #[test]
    fn rejects_different_commands() {
        let exe = r"C:\a.exe";
        assert!(!command_matches("", exe, &[], &no_env));
        assert!(!command_matches(r"C:\b.exe --startup", exe, &["--startup"], &no_env));
        assert!(!command_matches(r"C:\a.exe", exe, &["--startup"], &no_env));
        assert!(!command_matches(r"C:\a.exe --STARTUP", exe, &["--startup"], &no_env));
    }

    #[test]
    fn expands_only_complete_known_variables() {
        assert_eq!(expand_environment("%LOCALAPPDATA%\\a", &env), r"C:\Users\me\AppData\Local\a");
        assert_eq!(expand_environment("%NOPE%\\a", &env), r"%NOPE%\a");
        assert_eq!(expand_environment("100%", &env), "100%");
        assert_eq!(expand_environment("%%", &env), "%%");
    }

    #[test]
    fn counts_utf16_units() {
        let at_limit = "a".repeat(MAX_RUN_COMMAND_LENGTH);
        assert!(validate_length(&at_limit).is_ok());
        let emoji = format!("{}😀", "a".repeat(MAX_RUN_COMMAND_LENGTH - 1));
        assert_eq!(
            validate_length(&emoji),
            Err(CommandTooLong { actual_length: MAX_RUN_COMMAND_LENGTH + 1 })
        );
    }

    #[test]
    fn computes_sync_state() {
        let status = |state, desired_enabled| RegistrationStatus {
            state,
            desired_enabled,
            expected_command: String::new(),
            actual_command: None,
            error: None,
        };
        assert!(status(RegistrationState::Correct, true).in_sync());
        assert!(!status(RegistrationState::Correct, false).in_sync());
        assert!(status(RegistrationState::Absent, false).in_sync());
        assert!(!status(RegistrationState::Absent, true).in_sync());
        assert!(!status(RegistrationState::Stale, true).in_sync());
        assert!(!status(RegistrationState::Unreadable, false).in_sync());
    }
}
