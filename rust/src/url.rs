//! The subset of `urllib.parse.urlsplit` the launcher depends on.

use unicode_normalization::UnicodeNormalization;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlParts {
    pub scheme: String,
    pub netloc: String,
    pub path: String,
    pub query: String,
    pub fragment: String,
}

fn valid_scheme(value: &str) -> bool {
    let mut chars = value.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
}

/// `urlsplit`; `Err` for the `ValueError`s CPython raises.
pub fn split(url: &str) -> Result<UrlParts, ()> {
    let cleaned: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = cleaned.as_str();
    let mut scheme = String::new();
    if let Some(colon) = rest.find(':') {
        if colon > 0 && valid_scheme(&rest[..colon]) {
            scheme = rest[..colon].to_ascii_lowercase();
            rest = &rest[colon + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        rest = &after[end..];
        let opens = netloc.contains('[');
        let closes = netloc.contains(']');
        if opens != closes {
            return Err(());
        }
        if !netloc.is_ascii() {
            // CPython rejects hosts whose NFKC form introduces URL delimiters.
            let candidate = netloc.replace(['@', ':', '#', '?'], "");
            let normalized: String = candidate.nfkc().collect();
            if normalized != candidate && normalized.contains(['/', '?', '#', '@', ':']) {
                return Err(());
            }
        }
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Ok(UrlParts {
        scheme,
        netloc: netloc.to_string(),
        path: path.to_string(),
        query: query.to_string(),
        fragment: fragment.to_string(),
    })
}

impl UrlParts {
    fn user_info(&self) -> Option<&str> {
        self.netloc.rfind('@').map(|at| &self.netloc[..at])
    }

    fn host_port(&self) -> &str {
        match self.netloc.rfind('@') {
            Some(at) => &self.netloc[at + 1..],
            None => &self.netloc,
        }
    }

    /// Whether either the username or password component is present.
    pub fn has_credentials(&self) -> bool {
        self.user_info().is_some()
    }

    fn split_host_port(&self) -> (&str, Option<&str>) {
        let value = self.host_port();
        if let Some(open) = value.find('[') {
            let after_open = &value[open + 1..];
            let Some(close) = after_open.find(']') else {
                return (after_open, None);
            };
            let host = &after_open[..close];
            let tail = &after_open[close + 1..];
            return (host, tail.split_once(':').map(|(_, port)| port));
        }
        match value.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (value, None),
        }
    }

    /// Lower-cased host; empty when absent.
    pub fn hostname(&self) -> String {
        self.split_host_port().0.to_lowercase()
    }

    /// `SplitResult.port`: `Err` for a malformed or out-of-range port.
    pub fn port(&self) -> Result<Option<u32>, ()> {
        match self.split_host_port().1 {
            None | Some("") => Ok(None),
            Some(text) => {
                if !text.bytes().all(|b| b.is_ascii_digit()) {
                    return Err(());
                }
                let value: u32 = text.parse().map_err(|_| ())?;
                if value > 65535 {
                    return Err(());
                }
                Ok(Some(value))
            }
        }
    }

    /// `urlunsplit`
    pub fn unsplit(&self) -> String {
        let mut url = String::new();
        if !self.netloc.is_empty() {
            url.push_str("//");
            url.push_str(&self.netloc);
            if !self.path.is_empty() && !self.path.starts_with('/') {
                url.push('/');
            }
        }
        url.push_str(&self.path);
        if !self.scheme.is_empty() {
            url = format!("{}:{url}", self.scheme);
        }
        if !self.query.is_empty() {
            url.push('?');
            url.push_str(&self.query);
        }
        if !self.fragment.is_empty() {
            url.push('#');
            url.push_str(&self.fragment);
        }
        url
    }
}

/// `urllib.parse.unquote` for ASCII percent escapes with UTF-8 decoding.
pub fn unquote(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() + 0 {
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
            if let Ok(decoded) = u8::from_str_radix(hex, 16) {
                output.push(decoded);
                index += 3;
                continue;
            }
        }
        output.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&output).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_a_complete_url() {
        let parts = split("HTTPS://Example.com:8443/a/b?x=1#frag").unwrap();
        assert_eq!(parts.scheme, "https");
        assert_eq!(parts.netloc, "Example.com:8443");
        assert_eq!(parts.path, "/a/b");
        assert_eq!(parts.query, "x=1");
        assert_eq!(parts.fragment, "frag");
        assert_eq!(parts.hostname(), "example.com");
        assert_eq!(parts.port(), Ok(Some(8443)));
    }

    #[test]
    fn detects_credentials_and_bad_ports() {
        assert!(split("https://u:p@example.com").unwrap().has_credentials());
        assert_eq!(split("https://example.com:abc").unwrap().port(), Err(()));
        assert_eq!(split("https://example.com:70000").unwrap().port(), Err(()));
        assert_eq!(split("https://example.com:").unwrap().port(), Ok(None));
    }

    #[test]
    fn handles_ipv6_hosts() {
        let parts = split("http://[::1]:80/").unwrap();
        assert_eq!(parts.hostname(), "::1");
        assert_eq!(parts.port(), Ok(Some(80)));
        assert!(split("http://[::1/").is_err());
    }

    #[test]
    fn unsplit_drops_empty_query_and_fragment() {
        assert_eq!(split("https://a.com/?#").unwrap().unsplit(), "https://a.com/");
        assert_eq!(split("https://a.com").unwrap().unsplit(), "https://a.com");
    }

    #[test]
    fn unquotes_percent_sequences() {
        assert_eq!(unquote("a%2Fb%zz%"), "a/b%zz%");
        assert_eq!(unquote("%ED%95%9C"), "한");
    }
}
