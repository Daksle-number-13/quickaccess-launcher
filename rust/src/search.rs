//! Pure, pre-indexed launcher search for the latency-sensitive popup path.
//!
//! No filesystem or network access. Callers rebuild the index when the config
//! changes, then reuse it for every keystroke while the popup is open.

use unicode_normalization::UnicodeNormalization;

use crate::model::{ItemType, LauncherItem};
use crate::pyish::{collapse_whitespace, is_space};
use crate::url;

const HANGUL_BASE: u32 = 0xAC00;
const HANGUL_END: u32 = 0xD7A3;
const HANGUL_INITIAL_BLOCK: u32 = 21 * 28;
const HANGUL_INITIALS: [char; 19] = [
    'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ',
    'ㅍ', 'ㅎ',
];

/// A compatibility-normalized, case-insensitive search value.
fn normalize(value: &str) -> String {
    let composed: String = value.nfkc().collect();
    caseless::default_case_fold_str(&composed)
}

fn initial_for(character: char) -> Option<char> {
    let codepoint = character as u32;
    if (HANGUL_BASE..=HANGUL_END).contains(&codepoint) {
        return Some(HANGUL_INITIALS[((codepoint - HANGUL_BASE) / HANGUL_INITIAL_BLOCK) as usize]);
    }
    if HANGUL_INITIALS.contains(&character) {
        return Some(character);
    }
    if (0x1100..0x1100 + HANGUL_INITIALS.len() as u32).contains(&codepoint) {
        return Some(HANGUL_INITIALS[(codepoint - 0x1100) as usize]);
    }
    None
}

/// The compact Korean initial-consonant projection of `value`.
fn hangul_initials(value: &str) -> String {
    value.nfc().filter_map(initial_for).collect()
}

/// Canonicalize a query token made only from Korean initial consonants.
fn canonical_initial_token(value: &str) -> Option<String> {
    let token: Option<String> = value.chars().map(initial_for).collect();
    token.filter(|text| !text.is_empty())
}

fn url_hostname(item: &LauncherItem) -> String {
    if item.item_type != ItemType::Url {
        return String::new();
    }
    url::split(&item.path).map(|parts| parts.hostname()).unwrap_or_default()
}

struct IndexedItem {
    input_index: usize,
    order: i64,
    name: String,
    path: String,
    hostname: String,
    name_initials: String,
    path_initials: String,
    hostname_initials: String,
}

struct QueryToken {
    text: String,
    initials: Option<String>,
}

/// An immutable text index over a launcher item slice.
pub struct SearchIndex {
    items: Vec<IndexedItem>,
}

impl SearchIndex {
    pub fn new(items: &[LauncherItem]) -> Self {
        let mut indexed: Vec<IndexedItem> = items
            .iter()
            .enumerate()
            .map(|(input_index, item)| {
                let hostname = url_hostname(item);
                IndexedItem {
                    input_index,
                    order: item.order,
                    name: collapse_whitespace(&normalize(&item.name)),
                    path: normalize(&item.path),
                    hostname: normalize(&hostname),
                    name_initials: hangul_initials(&item.name),
                    path_initials: hangul_initials(&item.path),
                    hostname_initials: hangul_initials(&hostname),
                }
            })
            .collect();
        indexed.sort_by_key(|value| (value.order, value.input_index));
        Self { items: indexed }
    }

    /// Input-slice indexes of matches in deterministic relevance and manual order.
    pub fn search(&self, query: &str) -> Vec<usize> {
        let normalized_query = collapse_whitespace(&normalize(query));
        if normalized_query.is_empty() {
            return self.items.iter().map(|value| value.input_index).collect();
        }
        let tokens: Vec<QueryToken> = normalized_query
            .split(is_space)
            .map(|token| QueryToken {
                text: token.to_string(),
                initials: canonical_initial_token(token),
            })
            .collect();
        let initial_phrase: Option<String> = tokens
            .iter()
            .map(|token| token.initials.as_deref())
            .collect::<Option<Vec<_>>>()
            .map(|parts| parts.concat());

        let mut matches: Vec<(u8, i64, i64, usize)> = Vec::new();
        for value in &self.items {
            let mut name_hits = 0_i64;
            let mut matched = true;
            for token in &tokens {
                let (in_name, in_path, in_hostname) = match &token.initials {
                    None => (
                        value.name.contains(&token.text),
                        value.path.contains(&token.text),
                        value.hostname.contains(&token.text),
                    ),
                    Some(initials) => (
                        value.name_initials.contains(initials.as_str()),
                        value.path_initials.contains(initials.as_str()),
                        value.hostname_initials.contains(initials.as_str()),
                    ),
                };
                if !(in_name || in_path || in_hostname) {
                    matched = false;
                    break;
                }
                if in_name {
                    name_hits += 1;
                }
            }
            if !matched {
                continue;
            }
            let phrase = initial_phrase.as_deref();
            let tier = if normalized_query == value.name || phrase == Some(&value.name_initials) {
                0
            } else if value.name.starts_with(&normalized_query)
                || phrase.is_some_and(|p| value.name_initials.starts_with(p))
            {
                1
            } else if name_hits == tokens.len() as i64 {
                2
            } else if name_hits > 0 {
                3
            } else {
                4
            };
            matches.push((tier, -name_hits, value.order, value.input_index));
        }
        matches.sort_unstable();
        matches.into_iter().map(|value| value.3).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ItemType, LauncherItem};

    fn item(name: &str, path: &str, item_type: ItemType, order: i64) -> LauncherItem {
        LauncherItem::new("", name, path, item_type, order).unwrap()
    }

    fn names(items: &[LauncherItem], query: &str) -> Vec<String> {
        SearchIndex::new(items)
            .search(query)
            .into_iter()
            .map(|index| items[index].name.clone())
            .collect()
    }

    fn sample() -> Vec<LauncherItem> {
        vec![
            item("프로젝트 문서", r"C:\work\docs", ItemType::Folder, 0),
            item("다운로드", r"C:\Users\me\Downloads", ItemType::Folder, 1),
            item("문서", r"C:\Users\me\Documents", ItemType::Folder, 2),
            item("GitHub", "https://github.com/x", ItemType::Url, 3),
            item("품질 보고서", r"C:\문서함\report.xlsx", ItemType::File, 4),
        ]
    }

    #[test]
    fn empty_query_returns_manual_order() {
        let items = sample();
        assert_eq!(names(&items, "   ").len(), items.len());
        assert_eq!(names(&items, "")[0], "프로젝트 문서");
    }

    #[test]
    fn exact_name_beats_prefix_and_substring() {
        assert_eq!(names(&sample(), "문서")[0], "문서");
        assert_eq!(names(&sample(), "문서")[1], "프로젝트 문서");
    }

    #[test]
    fn matches_hangul_initials_with_either_jamo_form() {
        let items = sample();
        assert_eq!(names(&items, "ㄷㅇㄹㄷ"), ["다운로드"]);
        assert_eq!(names(&items, "\u{1103}\u{110b}\u{1105}\u{1103}"), ["다운로드"]);
        assert_eq!(names(&items, "ㅁㅅ")[0], "문서");
    }

    #[test]
    fn matches_paths_and_hostnames_at_a_lower_tier() {
        let items = sample();
        assert_eq!(names(&items, "report"), ["품질 보고서"]);
        assert_eq!(names(&items, "GITHUB.COM"), ["GitHub"]);
        assert_eq!(names(&items, "downloads"), ["다운로드"]);
    }

    #[test]
    fn requires_every_token() {
        let items = sample();
        assert_eq!(names(&items, "프로젝트   문서"), ["프로젝트 문서"]);
        assert!(names(&items, "프로젝트 없음").is_empty());
    }

    #[test]
    fn normalizes_compatibility_forms_and_case() {
        let items = vec![item("Report ＡＢＣ", r"C:\a", ItemType::File, 0)];
        assert_eq!(names(&items, "report abc").len(), 1);
        assert_eq!(names(&items, "ＲＥＰＯＲＴ").len(), 1);
        let eszett = vec![item("Straße", r"C:\b", ItemType::File, 0)];
        assert_eq!(names(&eszett, "STRASSE").len(), 1);
    }

    #[test]
    fn ties_keep_manual_order() {
        let items = vec![
            item("b 공통", r"C:\b", ItemType::File, 1),
            item("a 공통", r"C:\a", ItemType::File, 0),
        ];
        assert_eq!(names(&items, "공통"), ["a 공통", "b 공통"]);
    }

    #[test]
    fn ignores_hostnames_of_local_items() {
        let items = vec![item("x", "https://only-a-file-name", ItemType::File, 0)];
        assert_eq!(names(&items, "only-a-file").len(), 1);
        assert_eq!(url_hostname(&items[0]), "");
    }
}
