# QuickAccess Rust port

A native rewrite of the Python launcher using Win32, Direct2D and DirectWrite.
It shares `%APPDATA%\QuickAccess\items.json`, the single-instance mutex and the
`HKCU\...\Run` value with the Python releases, so only one of them can run at a time.

Requires Rust with the MSVC toolchain and the Windows SDK.

```powershell
cargo test                     # unit tests and Python parity tests
cargo build --release          # target\release\QuickAccess.exe
```

`.cargo/config.toml` links the C runtime statically, so the EXE runs without the
Visual C++ Redistributable. Public builds come from `..\build-rust.ps1`, which
also strips local paths and writes the release manifest and license bundle;
see `docs/RELEASE.md`.

| Option | Purpose |
| --- | --- |
| `--smoke-test` | Isolated start and clean exit, with a temporary config and `ctrl+alt+f23/f24` |
| `--capture <dir>` | Render every screen offscreen to PNG. Native edit text is not included |
| `--benchmark <file> [--items N]` | Record popup timings as JSON. The popup briefly appears on screen |

`tests-data/python_parity.json` holds answers recorded from the Python code.
Regenerate it from the repository root with `python devtools/generate_rust_parity.py`.
