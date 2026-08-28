"""Best-effort, origin-pinned release checks for QuickAccess.

The service never raises and is safe to run away from the Tk thread.  Results
retain the historical ``available`` flag while also exposing a precise status
for a future manual-check UI.  URLs returned by the API are only surfaced when
they point back to this repository on GitHub.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_REPO = "Daksle-number-13/quickaccess-launcher"
DEFAULT_TIMEOUT_SECONDS = 4.0
_RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpdateCheckStatus(str, Enum):
    """Stable states suitable for automatic and user-initiated checks."""

    LATEST = "latest"
    UPDATE_AVAILABLE = "update_available"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    available: bool
    latest_version: str | None = None
    release_url: str | None = None
    status: UpdateCheckStatus | None = None
    asset_url: str | None = None
    asset_size: int | None = None
    asset_digest: str | None = None
    error_reason: str | None = None

    def __post_init__(self) -> None:
        # Keep callers constructing the original three-field result compatible.
        if self.status is None:
            inferred = (
                UpdateCheckStatus.UPDATE_AVAILABLE
                if self.available
                else UpdateCheckStatus.LATEST
            )
            object.__setattr__(self, "status", inferred)


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _default_fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "QuickAccessLauncher",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS host
        # GitHub's latest-release response is small.  Bound untrusted input so a
        # proxy or unexpected endpoint cannot make the resident process allocate
        # an arbitrary payload.
        return response.read(1_048_577)


def _safe_github_url(candidate: object, *, repo: str, kind: str, tag: str) -> str | None:
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.casefold() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() != "github.com"
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        return None

    decoded_path = unquote(parts.path).rstrip("/")
    prefix = f"/{repo}/releases/{kind}/"
    if not decoded_path.casefold().startswith(prefix.casefold()):
        return None
    suffix = decoded_path[len(prefix) :]
    if kind == "tag":
        return candidate if suffix == tag else None
    if kind == "download":
        return candidate if suffix == f"{tag}/QuickAccess.exe" else None
    return None


def _trusted_asset(
    data: dict[str, object], *, repo: str, tag: str
) -> tuple[str | None, int | None, str | None]:
    assets = data.get("assets")
    if not isinstance(assets, list):
        return None, None, None
    for raw_asset in assets:
        if not isinstance(raw_asset, dict) or raw_asset.get("name") != "QuickAccess.exe":
            continue
        url = _safe_github_url(
            raw_asset.get("browser_download_url"),
            repo=repo,
            kind="download",
            tag=tag,
        )
        if url is None:
            continue
        raw_size = raw_asset.get("size")
        size = raw_size if isinstance(raw_size, int) and raw_size >= 0 else None
        raw_digest = raw_asset.get("digest")
        digest = raw_digest if isinstance(raw_digest, str) else None
        if digest is not None and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            digest = None
        return url, size, digest
    return None, None, None


def check_for_update(
    current_version: str,
    *,
    repo: str = DEFAULT_REPO,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fetch: Callable[[str, float], bytes] = _default_fetch,
) -> UpdateCheckResult:
    """Compare ``current_version`` against the repository's latest release.

    Failures are returned as data rather than raised.  Automatic callers may
    continue looking only at ``available``; manual callers can distinguish an
    up-to-date install, an offline machine, and an invalid server response.
    """

    current = _parse_version(current_version)
    if current is None or not _REPOSITORY_PATTERN.fullmatch(repo):
        return UpdateCheckResult(
            available=False,
            status=UpdateCheckStatus.ERROR,
            error_reason="invalid_request",
        )

    try:
        payload = fetch(_RELEASES_API.format(repo=repo), timeout)
        if len(payload) > 1_048_576:
            raise ValueError("release response is too large")
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("release response must be an object")
        tag = str(data.get("tag_name") or "").strip()
    except HTTPError:
        return UpdateCheckResult(
            available=False,
            status=UpdateCheckStatus.ERROR,
            error_reason="http_error",
        )
    except (URLError, TimeoutError, ConnectionError, OSError):
        return UpdateCheckResult(
            available=False,
            status=UpdateCheckStatus.OFFLINE,
            error_reason="offline",
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return UpdateCheckResult(
            available=False,
            status=UpdateCheckStatus.ERROR,
            error_reason="invalid_response",
        )
    except Exception:
        return UpdateCheckResult(
            available=False,
            status=UpdateCheckStatus.ERROR,
            error_reason="unexpected_error",
        )

    latest = _parse_version(tag)
    if latest is None:
        return UpdateCheckResult(
            available=False,
            latest_version=tag or None,
            status=UpdateCheckStatus.ERROR,
            error_reason="invalid_version",
        )

    release_url = _safe_github_url(
        data.get("html_url"), repo=repo, kind="tag", tag=tag
    )
    asset_url, asset_size, asset_digest = _trusted_asset(data, repo=repo, tag=tag)
    if latest <= current:
        return UpdateCheckResult(
            available=False,
            latest_version=tag,
            release_url=release_url,
            status=UpdateCheckStatus.LATEST,
            asset_url=asset_url,
            asset_size=asset_size,
            asset_digest=asset_digest,
        )

    return UpdateCheckResult(
        available=True,
        latest_version=tag,
        release_url=release_url,
        status=UpdateCheckStatus.UPDATE_AVAILABLE,
        asset_url=asset_url,
        asset_size=asset_size,
        asset_digest=asset_digest,
    )


__all__ = [
    "DEFAULT_REPO",
    "DEFAULT_TIMEOUT_SECONDS",
    "UpdateCheckResult",
    "UpdateCheckStatus",
    "check_for_update",
]
