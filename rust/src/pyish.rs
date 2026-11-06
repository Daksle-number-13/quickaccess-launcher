//! Text and Windows-path helpers that keep the semantics of the Python release.
//!
//! `items.json` files, duplicate detection and display names were all produced
//! by CPython's `str` and `ntpath`, so they are reproduced here rather than
//! approximated with `std::path`, which touches neither UNC roots nor
//! case-insensitive comparison in the same way.

/// Whether CPython's `str.isspace` treats `c` as whitespace.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

pub fn strip(value: &str) -> &str {
    value.trim_matches(is_space)
}

/// `" ".join(value.split())`
pub fn collapse_whitespace(value: &str) -> String {
    value
        .split(is_space)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// `ntpath.splitroot`
pub fn split_root(path: &str) -> (&str, &str, &str) {
    let norm = path.replace('/', "\\");
    if norm.starts_with('\\') {
        if norm[1..].starts_with('\\') {
            let start = if norm.len() >= 8
                && norm.is_char_boundary(8)
                && norm[..8].eq_ignore_ascii_case("\\\\?\\UNC\\")
            {
                8
            } else {
                2
            };
            let Some(first) = norm[start..].find('\\').map(|i| i + start) else {
                return (path, "", "");
            };
            let Some(second) = norm[first + 1..].find('\\').map(|i| i + first + 1) else {
                return (path, "", "");
            };
            return (&path[..second], &path[second..second + 1], &path[second + 1..]);
        }
        return ("", &path[..1], &path[1..]);
    }
    let mut chars = norm.char_indices();
    chars.next();
    if let Some((colon, ':')) = chars.next() {
        let drive_end = colon + 1;
        if norm[drive_end..].starts_with('\\') {
            return (
                &path[..drive_end],
                &path[drive_end..drive_end + 1],
                &path[drive_end + 1..],
            );
        }
        return (&path[..drive_end], "", &path[drive_end..]);
    }
    ("", "", path)
}

/// `ntpath.basename`
pub fn basename(path: &str) -> &str {
    let (drive, root, rest) = split_root(path);
    let tail_start = drive.len() + root.len();
    let tail = &path[tail_start..];
    let _ = rest;
    match tail.rfind(['\\', '/']) {
        Some(index) => &tail[index + 1..],
        None => tail,
    }
}

/// `ntpath.normpath`
pub fn normpath(path: &str) -> String {
    let (drive, root, rest) = split_root(path);
    let prefix = format!("{}{}", drive.replace('/', "\\"), root.replace('/', "\\"));
    let mut parts: Vec<&str> = Vec::new();
    for part in rest.split(['\\', '/']) {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|last| *last != "..") {
                    parts.pop();
                } else if root.is_empty() {
                    parts.push("..");
                }
            }
            other => parts.push(other),
        }
    }
    if prefix.is_empty() && parts.is_empty() {
        return ".".to_string();
    }
    format!("{prefix}{}", parts.join("\\"))
}

/// `ntpath.normcase`
pub fn normcase(path: &str) -> String {
    path.replace('/', "\\").to_lowercase()
}

/// `ntpath.normcase(ntpath.normpath(path.strip()))`, the app's local-target identity.
pub fn path_identity(path: &str) -> String {
    normcase(&normpath(strip(path)))
}

/// The extension returned by `ntpath.splitext`, including its dot.
pub fn extension(path: &str) -> &str {
    let separator = path.rfind(['\\', '/']).map_or(0, |index| index + 1);
    let colon = path.rfind(':').map_or(0, |index| index + 1);
    let name = &path[separator.max(colon)..];
    let leading = name.len() - name.trim_start_matches('.').len();
    match name[leading..].rfind('.') {
        Some(index) => &name[leading + index..],
        None => "",
    }
}

/// `ntpath.isabs` as shipped in Python 3.13, the interpreter of the v2 releases.
pub fn is_abs(path: &str) -> bool {
    let head: String = path.chars().take(3).collect::<String>().replace('/', "\\");
    if head.starts_with("\\\\") {
        return true;
    }
    let mut chars = head.chars();
    chars.next();
    chars.as_str().starts_with(":\\")
}

/// The name a Windows path shows when no explicit name was given.
pub fn display_name(path: &str) -> String {
    let trimmed = path.trim_end_matches(['\\', '/']);
    let name = basename(trimmed);
    if !name.is_empty() {
        name.to_string()
    } else if !trimmed.is_empty() {
        trimmed.to_string()
    } else {
        path.to_string()
    }
}

/// UTF-16 with terminating NUL for the wide Win32 APIs.
pub fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Middle-free prefix ellipsis in characters, for menu labels only.
pub fn ellipsize(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let mut shortened: String = value.chars().take(limit.saturating_sub(1)).collect();
    shortened.push('…');
    shortened
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_every_windows_root_form() {
        assert_eq!(split_root(r"C:\Users\a"), ("C:", "\\", r"Users\a"));
        assert_eq!(split_root("C:a"), ("C:", "", "a"));
        assert_eq!(split_root(r"\\server\share\x"), (r"\\server\share", "\\", "x"));
        assert_eq!(split_root(r"\\server"), (r"\\server", "", ""));
        assert_eq!(split_root(r"\x"), ("", "\\", "x"));
        assert_eq!(split_root("x"), ("", "", "x"));
        assert_eq!(
            split_root(r"\\?\UNC\server\share\x"),
            (r"\\?\UNC\server\share", "\\", "x")
        );
    }

    #[test]
    fn normalizes_paths_like_ntpath() {
        assert_eq!(normpath(r"C:/a/./b/../c\"), r"C:\a\c");
        assert_eq!(normpath(r"C:\.."), r"C:\");
        assert_eq!(normpath(""), ".");
        assert_eq!(normpath(r"..\a"), r"..\a");
        assert_eq!(normpath(r"\\srv\share\a\..\b"), r"\\srv\share\b");
        assert_eq!(path_identity(" C:/Users/A/ "), r"c:\users\a");
    }

    #[test]
    fn derives_display_names() {
        assert_eq!(display_name(r"C:\Users\한글\문서\"), "문서");
        assert_eq!(display_name(r"C:\"), "C:");
        assert_eq!(display_name(r"\\srv\share"), r"\\srv\share");
        assert_eq!(display_name("/"), "/");
        assert_eq!(basename("C:"), "");
    }

    #[test]
    fn extracts_extensions() {
        assert_eq!(extension(r"C:\a.b\file.TXT"), ".TXT");
        assert_eq!(extension(r"C:\a.b\file"), "");
        assert_eq!(extension(r"C:\a\.hidden"), "");
        assert_eq!(extension(r"C:\a\..x.y"), ".y");
    }

    #[test]
    fn classifies_absolute_paths() {
        assert!(is_abs(r"C:\x"));
        assert!(is_abs("C:/x"));
        assert!(is_abs(r"\\srv\x"));
        assert!(!is_abs(r"\x"));
        assert!(!is_abs("C:x"));
        assert!(!is_abs("::{GUID}"));
    }

    #[test]
    fn collapses_unicode_whitespace() {
        assert_eq!(collapse_whitespace("  a\u{3000}b\t c "), "a b c");
        assert_eq!(strip("\u{3000} x \n"), "x");
    }
}
