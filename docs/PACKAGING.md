# Packaging `FullPageCaptureBot.exe` for Windows

This document gives the **exact, step-by-step terminal commands** to compile the
application into a single, standalone Windows `.exe`.

> The build **must run on Windows** (PyInstaller cannot cross-compile a `.exe`
> from Linux/macOS). The commands below assume a Windows machine with a
> terminal (PowerShell or `cmd`).

---

## 1. One-time setup

```powershell
# 1a. Clone / open the project folder, then create a clean virtual environment
py -3.11 -m venv .venv
.venv\Scripts\activate

# 1b. Install every runtime + build dependency
pip install --upgrade pip
pip install -r requirements.txt

# 1c. (Optional, but recommended) verify the test-suite passes before packaging
python -m pytest
```

## 2. Build the single-file executable

Either run the provided one-click script:

```powershell
.\build_windows_exe.bat
```

…or run PyInstaller manually with the committed spec:

```powershell
pyinstaller FullPageCaptureBot.spec --noconfirm --clean
```

Output lands at:

```
dist\FullPageCaptureBot.exe      <- the single, standalone executable
```

## 3. First run on the target machine

The `.exe` embeds **all Python code + the Playwright driver**, but Chromium
itself is downloaded once at run time (it is ~150 MB and OS-specific). On the
very first launch, either:

* click **Install browser** in the app header (streams progress into the log), or
* run once from a terminal next to the exe:

```powershell
playwright install chromium
```

That's it - the app is now fully self-contained.

---

## What the `.spec` does (so you can reason about it)

| Concern | How it is handled |
|---|---|
| **Playwright driver** (the bundled Node.js automation engine) | `collect_all("playwright")` pulls the whole package - python code, node binary, driver JS - into the bundle. |
| **Browser binaries** (Chromium) | *Not* bundled. Downloaded at run time via `playwright install` or the in-app button; stored in `%LOCALAPPDATA%\ms-playwright`. |
| **App source** | `app/` is bundled as data and imported from `sys._MEIPASS`. |
| **Icon** | `assets/icon.ico` is baked into the exe. |
| **Single file** | `EXE(... a.binaries, a.datas ...)` with `onefile` semantics (`runtime_tmp_dir` extraction). |
| **No console window** | `console=False` (it is a GUI app; errors surface in the UI log). |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Executable doesn't exist at ...\ms-playwright\...` | Run `playwright install chromium` once (or the in-app **Install browser**). |
| Antivirus flags the one-file exe | This is normal for PyInstaller one-file builds. Either whitelist it, or rebuild with `--onedir` by removing the one-file arguments from the spec and shipping the `dist\FullPageCaptureBot\` folder instead. |
| Build is slow / huge | Expected - Qt + Playwright are large. The result is ~60-90 MB. |
| `ModuleNotFoundError: PyQt6...` at build time | Ensure the venv is activated and `pip install -r requirements.txt` succeeded. |
| Fonts look different in CI | The theme prefers `Segoe UI` on Windows; other platforms fall back gracefully. |

## Verifying the build (on Windows)

```powershell
# launch it
.\dist\FullPageCaptureBot.exe
```

Then paste a URL, choose a folder, press **Start Capture**, and confirm a PNG
appears in the folder. The bundled test-suite (`python -m pytest`) covers the
automation logic headlessly and is the fastest regression check.

---

## 4. (Optional) Sign the executable

An unsigned PyInstaller exe triggers Windows SmartScreen warnings. Sign it with a
code-signing certificate:

```powershell
tools\sign_windows_exe.bat  path\to\your-certificate.pfx
```

The script runs `signtool sign` (SHA-256, RFC-3161 timestamp) and then verifies.
Requirements: the Windows SDK `signtool` and a `.pfx` certificate.

## 5. Publish a release (enables the in-app auto-update check)

The app's **Check updates** button compares `app/version.py` against the latest
**GitHub Release** tag of `bestboygod021/screenmytools`. To make it work:

1. Build + (optionally) sign the exe as above.
2. Create a release with a semantic tag (e.g. `v1.1.0`) and attach
   `FullPageCaptureBot.exe`:

```powershell
gh release create v1.1.0 dist\FullPageCaptureBot.exe --title "v1.1.0" --notes "..."
```

3. Bump `app/version.py` on `main` so older installs detect the newer tag.

When a newer tag exists, the status bar shows *Update available* with a link to
the Releases page; otherwise it confirms you are up to date. The check is
best-effort and never blocks or crashes when offline.

## The SMTP password in a packaged build

The `.exe` talks to the Windows Credential Locker through `keyring`, which the
spec file already lists as a hidden import (it is imported lazily, so PyInstaller
cannot see it). Nothing else changes: the settings file stores an empty
`smtp_password`, `python -m app.cli secrets status` reports *Windows Credential
Locker*, and the password survives uninstalling the app - which is the point of
putting it there rather than in `settings.ini`.
