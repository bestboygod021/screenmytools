# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FullPage Capture Bot.

Builds a single, standalone ``FullPageCaptureBot.exe``.

The one non-obvious part is Playwright: its automation driver is a bundled
Node.js program and its browsers are downloaded *at run time*, so we must
(a) bundle the whole ``playwright`` package (driver included) and
(b) remember to run ``playwright install chromium`` once on the target machine
    (or use the in-app "Install browser" button).

Build from the repo root with::

    pyinstaller FullPageCaptureBot.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# --- bundle the entire Playwright package: python code + node driver + data ---
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

APP_ROOT = Path(SPECPATH)

datas = list(pw_datas) + [
    # application source package
    (str(APP_ROOT / "app"), "app"),
    # bundled assets (icon) - read via sys._MEIPASS at run time
    (str(APP_ROOT / "assets"), "assets"),
]

binaries = list(pw_binaries)

hiddenimports = list(pw_hidden) + [
    "app",
    "app.core",
    "app.core.engine",
    "app.core.settings",
    "app.core.url_utils",
    "app.core.runtime",
    "app.ui",
    "app.ui.theme",
    "app.ui.widgets",
    "app.ui.main_window",
    "app.worker",
    "app.version",
    # Pillow is imported lazily inside the stitching path
    "PIL",
    "PIL.Image",
    # keyring is imported lazily by app.core.secrets, and its OS backend is chosen
    # at run time - PyInstaller has to be told about both.
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "matplotlib",
        "numpy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    # --- single-file build: everything is embedded into one executable ---
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FullPageCaptureBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmp_dir=None,
    console=False,          # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(APP_ROOT / "assets" / "icon.ico"),
    uac_admin=False,
    uac_uiaccess=False,
)
