"""Best-effort check for a newer QuickAccess release on GitHub.

Runs entirely off the Tk thread and never raises.  A blocked or slow
network call -- common behind a corporate proxy -- must not delay startup
or interrupt the resident process, so every failure mode collapses to
"no update available" instead of propagating.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.request import Request, urlopen


DEFAULT_REPO = "Daksle-number-13/quickaccess-launcher"
DEFAULT_TIMEOUT_SECONDS = 4.0
_RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    available: bool
    latest_version: str | None = None
    release_url: str | None = None


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.search(text)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _default_fetch(url: str, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "QuickAccessLauncher"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS host
        return response.read()


def check_for_update(
    current_version: str,
    *,
    repo: str = DEFAULT_REPO,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fetch: Callable[[str, float], bytes] = _default_fetch,
) -> UpdateCheckResult:
    """Compare ``current_version`` against the repository's latest release.

    Any parsing, network, or unexpected error is treated the same as "no
    update available" so a firewall block or a malformed response can never
    surface as an error toast in the resident app.
    """

    current = _parse_version(current_version)
    if current is None:
        return UpdateCheckResult(available=False)

    try:
        payload = fetch(_RELEASES_API.format(repo=repo), timeout)
        data = json.loads(payload.decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        release_url = data.get("html_url")
    except Exception:
        return UpdateCheckResult(available=False)

    latest = _parse_version(tag)
    if latest is None or latest <= current:
        return UpdateCheckResult(available=False, latest_version=tag or None)

    return UpdateCheckResult(
        available=True,
        latest_version=tag,
        release_url=str(release_url) if release_url else None,
    )


__all__ = [
    "DEFAULT_REPO",
    "DEFAULT_TIMEOUT_SECONDS",
    "UpdateCheckResult",
    "check_for_update",
]
