"""Pure, pre-indexed launcher search for the latency-sensitive popup path.

The module deliberately performs no filesystem or network access.  Callers
rebuild an index when launcher configuration changes, then reuse it for every
keystroke while the popup is open.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import unicodedata
from urllib.parse import urlsplit

from .models import LauncherItem


_HANGUL_BASE = 0xAC00
_HANGUL_END = 0xD7A3
_HANGUL_INITIAL_BLOCK = 21 * 28
_HANGUL_INITIALS = tuple("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ")
_LEADING_JAMO_TO_INITIAL = {
    chr(0x1100 + index): initial
    for index, initial in enumerate(_HANGUL_INITIALS)
}
_COMPATIBILITY_INITIALS = frozenset(_HANGUL_INITIALS)


def _normalize(value: str) -> str:
    """Return a compatibility-normalized, case-insensitive search value."""

    return unicodedata.normalize("NFKC", value).casefold()


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _initial_for_character(character: str) -> str | None:
    codepoint = ord(character)
    if _HANGUL_BASE <= codepoint <= _HANGUL_END:
        return _HANGUL_INITIALS[
            (codepoint - _HANGUL_BASE) // _HANGUL_INITIAL_BLOCK
        ]
    if character in _COMPATIBILITY_INITIALS:
        return character
    return _LEADING_JAMO_TO_INITIAL.get(character)


def _hangul_initials(value: str) -> str:
    """Extract the compact Korean initial-consonant projection of ``value``."""

    result: list[str] = []
    for character in unicodedata.normalize("NFC", value):
        initial = _initial_for_character(character)
        if initial is not None:
            result.append(initial)
    return "".join(result)


def _canonical_initial_token(value: str) -> str | None:
    """Canonicalize a query token made only from Korean initial consonants."""

    result: list[str] = []
    for character in value:
        initial = _initial_for_character(character)
        if initial is None:
            return None
        result.append(initial)
    return "".join(result) if result else None


def _url_hostname(item: LauncherItem) -> str:
    if item.type != "url":
        return ""
    try:
        return urlsplit(item.path).hostname or ""
    except ValueError:
        return ""


@dataclass(frozen=True, slots=True)
class _IndexedItem:
    item: LauncherItem
    input_index: int
    name: str
    path: str
    hostname: str
    name_initials: str
    path_initials: str
    hostname_initials: str


@dataclass(frozen=True, slots=True)
class _QueryToken:
    text: str
    initials: str | None


class LauncherSearchIndex:
    """An immutable text index that preserves references to launcher items."""

    def __init__(self, items: Iterable[LauncherItem]) -> None:
        indexed: list[_IndexedItem] = []
        for input_index, item in enumerate(items):
            hostname = _url_hostname(item)
            indexed.append(
                _IndexedItem(
                    item=item,
                    input_index=input_index,
                    name=_collapse_whitespace(_normalize(item.name)),
                    path=_normalize(item.path),
                    hostname=_normalize(hostname),
                    name_initials=_hangul_initials(item.name),
                    path_initials=_hangul_initials(item.path),
                    hostname_initials=_hangul_initials(hostname),
                )
            )
        self._items = tuple(
            sorted(indexed, key=lambda value: (value.item.order, value.input_index))
        )

    def search(self, query: str) -> list[LauncherItem]:
        """Return matching items in deterministic relevance and manual order."""

        normalized_query = _collapse_whitespace(_normalize(query))
        if not normalized_query:
            return [value.item for value in self._items]

        tokens = tuple(
            _QueryToken(token, _canonical_initial_token(token))
            for token in normalized_query.split()
        )
        initial_phrase: str | None = None
        if all(token.initials is not None for token in tokens):
            initial_phrase = "".join(token.initials or "" for token in tokens)

        matches: list[tuple[int, int, int, int, LauncherItem]] = []
        for value in self._items:
            name_hits = 0
            matched = True
            for token in tokens:
                if token.initials is None:
                    in_name = token.text in value.name
                    in_path = token.text in value.path
                    in_hostname = token.text in value.hostname
                else:
                    in_name = token.initials in value.name_initials
                    in_path = token.initials in value.path_initials
                    in_hostname = token.initials in value.hostname_initials
                if not (in_name or in_path or in_hostname):
                    matched = False
                    break
                if in_name:
                    name_hits += 1
            if not matched:
                continue

            if normalized_query == value.name or (
                initial_phrase is not None and initial_phrase == value.name_initials
            ):
                tier = 0
            elif value.name.startswith(normalized_query) or (
                initial_phrase is not None
                and value.name_initials.startswith(initial_phrase)
            ):
                tier = 1
            elif name_hits == len(tokens):
                tier = 2
            elif name_hits:
                tier = 3
            else:
                tier = 4

            matches.append(
                (
                    tier,
                    -name_hits,
                    value.item.order,
                    value.input_index,
                    value.item,
                )
            )

        matches.sort(key=lambda value: value[:4])
        return [value[4] for value in matches]


def search_launcher_items(
    items: Iterable[LauncherItem],
    query: str,
) -> list[LauncherItem]:
    """Convenience wrapper for one-off searches.

    Interactive callers should retain :class:`LauncherSearchIndex` instead of
    rebuilding normalized fields for every keystroke.
    """

    return LauncherSearchIndex(items).search(query)


__all__ = ["LauncherSearchIndex", "search_launcher_items"]
