//! Best-effort, origin-pinned release checks.
//!
//! URLs from the API are surfaced only when they point back to this repository
//! on GitHub. Failures are returned as data rather than raised.

use serde_json::Value;

use crate::url;

pub const DEFAULT_REPO: &str = "Daksle-number-13/quickaccess-launcher";
const MAX_VERSION_LENGTH: usize = 64;
pub const MAX_RESPONSE_BYTES: usize = 1_048_576;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FetchError {
    Http,
    Offline,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct UpdateCheckResult {
    pub available: bool,
    pub latest_version: Option<String>,
    pub release_url: Option<String>,
}

pub fn releases_api_url(repo: &str) -> String {
    format!("https://api.github.com/repos/{repo}/releases/latest")
}

pub fn parse_version(text: &str) -> Option<(u64, u64, u64)> {
    let text = text.trim();
    if text.len() > MAX_VERSION_LENGTH {
        return None;
    }
    let digits = text.strip_prefix(['v', 'V']).unwrap_or(text);
    let mut parts = digits.split('.');
    let mut number = || {
        let part = parts.next()?;
        (!part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
            .then(|| part.parse::<u64>().ok())
            .flatten()
    };
    let version = (number()?, number()?, number()?);
    parts.next().is_none().then_some(version)
}

fn valid_repository(repo: &str) -> bool {
    let valid_part = |part: &str| {
        !part.is_empty()
            && part
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
    };
    matches!(repo.split_once('/'), Some((owner, name)) if valid_part(owner) && valid_part(name))
}

/// Accept only `https://github.com/<repo>/releases/tag/<tag>` with no extras.
fn safe_release_url(candidate: Option<&Value>, repo: &str, tag: &str) -> Option<String> {
    let text = candidate?.as_str().filter(|text| !text.is_empty())?;
    let parts = url::split(text).ok()?;
    let port_ok = matches!(parts.port(), Ok(None | Some(443)));
    if parts.scheme != "https"
        || parts.hostname() != "github.com"
        || !port_ok
        || parts.has_credentials()
        || !parts.query.is_empty()
        || !parts.fragment.is_empty()
    {
        return None;
    }
    let decoded = url::unquote(&parts.path);
    let decoded = decoded.trim_end_matches('/');
    let prefix = format!("/{repo}/releases/tag/");
    if decoded.len() < prefix.len() || !decoded.is_char_boundary(prefix.len()) {
        return None;
    }
    let (head, suffix) = decoded.split_at(prefix.len());
    (head.eq_ignore_ascii_case(&prefix) && suffix == tag).then(|| text.to_string())
}

/// Compare `current` against the repository's latest release.
pub fn check_for_update(
    current: &str,
    repo: &str,
    fetch: &dyn Fn(&str) -> Result<Vec<u8>, FetchError>,
) -> UpdateCheckResult {
    let unavailable = UpdateCheckResult::default;
    let Some(current) = parse_version(current).filter(|_| valid_repository(repo)) else {
        return unavailable();
    };
    let Ok(payload) = fetch(&releases_api_url(repo)) else {
        return unavailable();
    };
    if payload.len() > MAX_RESPONSE_BYTES {
        return unavailable();
    }
    let Ok(Value::Object(data)) = serde_json::from_slice::<Value>(&payload) else {
        return unavailable();
    };
    let tag = data
        .get("tag_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let Some(latest) = parse_version(&tag) else {
        return UpdateCheckResult {
            latest_version: (!tag.is_empty()).then_some(tag),
            ..unavailable()
        };
    };
    UpdateCheckResult {
        available: latest > current,
        release_url: safe_release_url(data.get("html_url"), repo, &tag),
        latest_version: Some(tag),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn respond(body: Value) -> impl Fn(&str) -> Result<Vec<u8>, FetchError> {
        move |url: &str| {
            assert_eq!(url, releases_api_url(DEFAULT_REPO));
            Ok(body.to_string().into_bytes())
        }
    }

    fn tag_url(tag: &str) -> String {
        format!("https://github.com/{DEFAULT_REPO}/releases/tag/{tag}")
    }

    #[test]
    fn parses_versions_strictly() {
        assert_eq!(parse_version(" v1.2.3 "), Some((1, 2, 3)));
        assert_eq!(parse_version("V10.0.1"), Some((10, 0, 1)));
        for bad in ["1.2", "1.2.3.4", "1.2.x", "", "v", "1..3", &"9".repeat(65)] {
            assert_eq!(parse_version(bad), None, "{bad}");
        }
    }

    #[test]
    fn reports_a_newer_release() {
        let fetch = respond(json!({"tag_name": "v3.1.0", "html_url": tag_url("v3.1.0")}));
        let result = check_for_update("3.0.0", DEFAULT_REPO, &fetch);
        assert!(result.available);
        assert_eq!(result.latest_version.as_deref(), Some("v3.1.0"));
        assert_eq!(result.release_url, Some(tag_url("v3.1.0")));
    }

    #[test]
    fn compares_numerically_not_lexically() {
        let fetch = respond(json!({"tag_name": "v3.10.0"}));
        assert!(check_for_update("3.9.0", DEFAULT_REPO, &fetch).available);
        let fetch = respond(json!({"tag_name": "v3.0.0"}));
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &fetch).available);
        let fetch = respond(json!({"tag_name": "v2.9.9"}));
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &fetch).available);
    }

    #[test]
    fn drops_urls_outside_the_repository() {
        for bad in [
            "https://evil.example/x".to_string(),
            "http://github.com/x".to_string(),
            format!("https://github.com/other/repo/releases/tag/v9.0.0"),
            format!("https://github.com/{DEFAULT_REPO}/releases/tag/v8.0.0"),
            format!("{}?x=1", tag_url("v9.0.0")),
            format!("{}#f", tag_url("v9.0.0")),
            format!("https://u@github.com/{DEFAULT_REPO}/releases/tag/v9.0.0"),
            format!("https://github.com:8443/{DEFAULT_REPO}/releases/tag/v9.0.0"),
        ] {
            let fetch = respond(json!({"tag_name": "v9.0.0", "html_url": bad}));
            let result = check_for_update("3.0.0", DEFAULT_REPO, &fetch);
            assert!(result.available);
            assert_eq!(result.release_url, None);
        }
    }

    #[test]
    fn failures_never_report_an_update() {
        let offline = |_: &str| Err(FetchError::Offline);
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &offline).available);
        let http = |_: &str| Err(FetchError::Http);
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &http).available);
        let garbage = |_: &str| Ok(b"<html>".to_vec());
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &garbage).available);
        let huge = |_: &str| Ok(vec![b' '; MAX_RESPONSE_BYTES + 1]);
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &huge).available);
        let not_object = respond(json!([1]));
        assert!(!check_for_update("3.0.0", DEFAULT_REPO, &not_object).available);
        let bad_tag = respond(json!({"tag_name": "latest"}));
        let result = check_for_update("3.0.0", DEFAULT_REPO, &bad_tag);
        assert!(!result.available);
        assert_eq!(result.latest_version.as_deref(), Some("latest"));
    }

    #[test]
    fn rejects_invalid_requests_without_fetching() {
        let never = |_: &str| -> Result<Vec<u8>, FetchError> { panic!("must not fetch") };
        assert!(!check_for_update("dev", DEFAULT_REPO, &never).available);
        assert!(!check_for_update("3.0.0", "a/b/c", &never).available);
        assert!(!check_for_update("3.0.0", "no-slash", &never).available);
    }
}
