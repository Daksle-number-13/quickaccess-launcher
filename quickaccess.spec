# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build recipe.

CustomTkinter normally recommends an onedir bundle.  Collecting its complete
package data here makes the product requirement (one executable) explicit and
repeatable.  Always validate the result on a clean Windows machine.
"""

from PyInstaller.utils.hooks import collect_data_files


ctk_datas = collect_data_files("customtkinter", include_py_files=False)
hidden_imports = [
    "PIL.Image",
    "PIL.IcoImagePlugin",
    "pythoncom",
    "pystray._win32",
    "pywintypes",
    "win32api",
    "win32com.client",
    "win32con",
    "win32event",
    "win32gui",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=ctk_datas,
    hiddenimports=hidden_imports,
    hookspath=["pyinstaller_hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy",
        "PIL.ImageCms",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "PIL._avif",
        "PIL._imagingcms",
        "PIL._imagingft",
        "PIL._imagingmath",
        "PIL._webp",
        "Pythonwin",
        "win32ui",
    ],
    noarchive=False,
    optimize=0,
)


def keep_tcl_runtime(entry):
    """Keep the Tcl resources used by this Korean/English desktop app.

    A stock Tk collection contains more than 700 timezone and translation
    files.  Extracting each file from a one-file executable is expensive on
    virus-scanned corporate PCs.  QuickAccess does not expose Tcl clock or
    locale APIs, but we retain the local Korean/English messages and common
    Seoul/UTC timezone aliases as a conservative runtime fallback.
    """

    destination = entry[0].replace("\\", "/").casefold()
    if destination.startswith("_tcl_data/tzdata/"):
        return destination in {
            "_tcl_data/tzdata/asia/seoul",
            "_tcl_data/tzdata/etc/gmt",
            "_tcl_data/tzdata/etc/utc",
            "_tcl_data/tzdata/gmt",
            "_tcl_data/tzdata/rok",
            "_tcl_data/tzdata/utc",
        }
    if destination.startswith("_tcl_data/msgs/"):
        filename = destination.rsplit("/", 1)[-1]
        return filename.startswith(("en", "ko"))
    return True


a.datas = [entry for entry in a.datas if keep_tcl_runtime(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QuickAccess",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    version="version_info.txt",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
