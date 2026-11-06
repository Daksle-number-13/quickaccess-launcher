"""Record the Python implementation's answers for the Rust parity test.

Run from the repository root: python devtools/generate_rust_parity.py
"""

from __future__ import annotations

import json
import ntpath
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickaccess.models import LauncherConfig, LauncherItem, normalize_web_url  # noqa: E402
from quickaccess.search import LauncherSearchIndex  # noqa: E402
from quickaccess.services.hotkeys import HotkeyParseError, parse_hotkey  # noqa: E402
from quickaccess.services.icons import icon_key  # noqa: E402
from quickaccess.services.monitor import Point, Rect, Size, clamp_window_to_work_area  # noqa: E402
from quickaccess.services.startup import (  # noqa: E402
    _format_startup_command,
    _split_windows_command_line,
    startup_command_matches,
)
from quickaccess.services.update_check import _parse_version  # noqa: E402

CONFIGS = [
    [],
    [{"name": "a", "path": "C:\\a"}, {"path": "https://example.com/x"}, {"name": "n"}, 7, None],
    {"items": []},
    {"schema_version": 1, "columns": "4", "run_on_startup": "off", "items": []},
    {"columns": True, "hotkey": " CTRL+K ", "quick_add_hotkey": "", "items": []},
    {"columns": 9.7, "welcome_shown": 1, "check_updates": "yes", "items": []},
    {"columns": -3, "appearance_mode": "DARK", "last_update_notice": " v1 ", "items": []},
    {"columns": "1_0", "appearance_mode": "x", "run_on_startup": 2, "items": []},
    {
        "items": [
            {"id": "a", "path": "C:\\a", "order": 5},
            {"id": "b", "path": "C:\\b", "order": "1"},
            {"id": "c", "path": "C:\\c", "order": 5.9},
            {"id": "d", "path": "C:\\d", "order": -3},
            {"id": "e", "path": "C:\\e", "order": True},
            {"id": "f", "path": "  ", "order": 0},
            {"id": "g", "name": "  ", "path": " C:\\Users\\한글\\문서\\ ", "type": "folder"},
            {"id": "h", "type": "url", "path": "ftp://x"},
            {"id": "i", "type": "url", "path": "Example.com/a?b=1#c"},
            {"id": "j", "type": "weird", "path": "http://x.y"},
            {"id": "k", "type": "weird", "path": "C:\\z"},
        ]
    },
]
INVALID_CONFIGS = [
    3,
    "x",
    {"items": "x"},
    {"schema_version": 0, "items": []},
    {"schema_version": True, "items": []},
    {"schema_version": 1.0, "items": []},
]

URLS = [
    "example.com", " Example.com/a ", "HTTP://x.y/?q=1#f", "https://a.com/?#", "https://a.com",
    "http://[::1]:80/", "http://[::1/", "https://한글.com/경로", "https://x.y:443/a", "x.y:8080",
    "", "ftp://x.y", "https://", "https://u:p@x.y", "https://u@x.y", "https://x.y:abc",
    "https://x.y:70000", "a b.com", "a\\b.com", "mailto:a@b.c", "http:/x", "HTTPS://A.B/C",
]

ITEMS = [
    ("프로젝트 문서", "C:\\work\\docs", "folder"),
    ("다운로드", "C:\\Users\\me\\Downloads", "folder"),
    ("문서", "C:\\Users\\me\\Documents", "folder"),
    ("GitHub", "https://github.com/x", "url"),
    ("품질 보고서", "C:\\문서함\\report.xlsx", "file"),
    ("Report ＡＢＣ", "C:\\a b\\Straße.txt", "file"),
    ("한글 English 혼합", "\\\\srv\\share\\파일", "file"),
    ("네이버", "https://www.naver.com/", "url"),
]
QUERIES = [
    "", "  ", "문서", "ㅁㅅ", "ㄷㅇㄹㄷ", "\u1103\u110b\u1105\u1103", "ㅍㄹㅈㅌ ㅁㅅ", "report", "REPORT",
    "ａｂｃ", "strasse", "github.com", "naver", "users", "프로젝트   문서", "프로젝트 없음", "ㅎ", "ㅁ",
    "c:\\", "English 혼합", "ㅎㄱ", "ㄴㅇㅂ", "문", "보고서 xlsx", "서",
]

HOTKEYS = [
    "ctrl+space", "Shift + CONTROL+Space", "alt+a", "win+f24", "ctrl+numpad7", "super+;",
    "ctrl+page up", "option+return", "ctrl+del", "ctrl+`", "ctrl+\\", "windows+1",
    "", "  ", "space", "ctrl", "ctrl+", "ctrl+ctrl+a", "ctrl+a+b", "ctrl+f25", "ctrl+f0",
    "ctrl+numpad10", "ctrl+é",
]

PATHS = [
    "C:\\Users\\a", "C:/a/./b/../c\\", "C:\\..", "", "..\\a", "\\\\srv\\share\\a\\..\\b", "\\\\srv",
    "\\x", "C:a", "a\\b.txt", "C:\\a.b\\file.TXT", "C:\\a\\.hidden", "C:\\a\\..x.y",
    "\\\\?\\UNC\\srv\\share\\x", "한글\\폴더\\", "C:", "/", "a/b/", "..", ".", "a\\..\\..\\b", "C:\\a\\\\b",
]
ICON_PATHS = ["C:\\A\\B.TXT", "C:\\a\\noext", "C:/A/App.EXE", "C:\\x.LNK", "C:\\y.ico", " C:\\z.pdf ", "\\\\srv\\a.lnk"]

COMMANDS = [
    ["C:\\a.exe", "--startup"],
    ["C:\\Program Files\\a.exe", "--startup"],
    ["a", ""],
    ["a", 'x"y'],
    ["a", "C:\\my dir\\"],
    ["C:\\한글 폴더\\a.exe"],
    ["a\\", "b\\c"],
]
LINES = [
    '"C:\\Program Files\\a.exe" --startup',
    "  a   b\t",
    "   ",
    '"a""b" c',
    'a\\\\"b c"',
    'a\\"b',
    '"a\\\\" b',
    "a\\\\b",
]
MATCHES = [
    ("C:\\Users\\Me\\Q.exe --startup", "C:\\Users\\Me\\Q.exe", ["--startup"]),
    ('"c:\\users\\me\\q.exe" --startup', "C:\\Users\\Me\\Q.exe", ["--startup"]),
    ("C:/Users/Me/./Q.exe --startup", "C:\\Users\\Me\\Q.exe", ["--startup"]),
    ("\\\\?\\C:\\Users\\Me\\Q.exe --startup", "C:\\Users\\Me\\Q.exe", ["--startup"]),
    ("\\\\?\\UNC\\srv\\s\\a.exe --startup", "\\\\srv\\s\\a.exe", ["--startup"]),
    ("C:\\b.exe --startup", "C:\\a.exe", ["--startup"]),
    ("C:\\a.exe", "C:\\a.exe", ["--startup"]),
    ("C:\\a.exe --STARTUP", "C:\\a.exe", ["--startup"]),
    ("", "C:\\a.exe", []),
]
VERSIONS = ["1.2.3", " v1.2.3 ", "V10.0.1", "1.2", "1.2.3.4", "1.2.x", "", "v", "1..3", "9" * 65]
RECTS = [(0, 0, 1920, 1040), (-1920, -200, 0, 880), (100, 100, 200, 200)]
CLAMPS = [((-5, 870), (300, 200)), ((-3000, -900), (300, 200)), ((150, 150), (500, 500)), ((1900, 1000), (10, 10))]


def attempt(function, *args):
    try:
        return {"ok": function(*args)}
    except (ValueError, TypeError, HotkeyParseError):
        return {"error": True}


def hotkey_answer(value: str) -> dict:
    parsed = parse_hotkey(value)
    return {"modifiers": parsed.modifiers, "virtual_key": parsed.virtual_key, "canonical": parsed.canonical}


def clamp_answer(rect, cursor, size) -> list[int]:
    point = clamp_window_to_work_area(Point(*cursor), Size(*size), Rect(*rect))
    return [point.x, point.y]


def popup_cases():
    try:
        from quickaccess.ui.popup import popup_dimensions
    except ImportError:
        return None
    cases = []
    for count in (0, 1, 2, 6, 20, 40, 200):
        for columns in (2, 3, 5):
            for rect in ((0, 0, 1920, 1040), (0, 0, 1920, 300), (0, 0, 816, 616), (0, 0, 328, 300)):
                for scale in (1.0, 1.25, 1.5, 2.0):
                    cases.append(
                        {
                            "count": count,
                            "columns": columns,
                            "rect": rect,
                            "scale": scale,
                            "answer": popup_dimensions(count, columns, Rect(*rect), scale),
                        }
                    )
    return cases


def main() -> None:
    items = [LauncherItem(id=f"i{i}", name=n, path=p, type=t, order=i) for i, (n, p, t) in enumerate(ITEMS)]
    index = LauncherSearchIndex(items)
    document = {
        "configs": [
            {"input": data, "answer": answer, "rendered": json.dumps(answer, ensure_ascii=False, indent=2) + "\n"}
            for data in CONFIGS
            for answer in [LauncherConfig.from_data(data, user_home="C:\\Users\\tester").to_dict()]
        ],
        "invalid_configs": INVALID_CONFIGS,
        "urls": [{"input": url, "answer": attempt(normalize_web_url, url)} for url in URLS],
        "items": [{"id": f"i{i}", "name": n, "path": p, "type": t} for i, (n, p, t) in enumerate(ITEMS)],
        "searches": [{"query": q, "answer": [item.id for item in index.search(q)]} for q in QUERIES],
        "hotkeys": [{"input": h, "answer": attempt(hotkey_answer, h)} for h in HOTKEYS],
        "paths": [
            {
                "input": p,
                "normpath": ntpath.normpath(p),
                "basename": ntpath.basename(p),
                "splitroot": list(ntpath.splitroot(p)),
                "extension": ntpath.splitext(p)[1],
            }
            for p in PATHS
        ],
        "icon_keys": [{"input": p, "answer": icon_key(p, "file")} for p in ICON_PATHS],
        "commands": [{"input": c, "answer": _format_startup_command(c[0], c[1:])} for c in COMMANDS],
        "command_lines": [{"input": line, "answer": list(_split_windows_command_line(line))} for line in LINES],
        "command_matches": [
            {
                "actual": actual,
                "executable": executable,
                "arguments": arguments,
                "answer": startup_command_matches(actual, executable, arguments, environment={}),
            }
            for actual, executable, arguments in MATCHES
        ],
        "versions": [{"input": v, "answer": _parse_version(v)} for v in VERSIONS],
        "clamps": [
            {"rect": r, "cursor": c, "size": s, "answer": clamp_answer(r, c, s)}
            for r in RECTS
            for c, s in CLAMPS
        ],
        "popup_dimensions": popup_cases(),
    }
    target = Path(__file__).resolve().parents[1] / "rust" / "tests-data" / "python_parity.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{target}: popup_dimensions={'yes' if document['popup_dimensions'] else 'skipped'}")


if __name__ == "__main__":
    main()
