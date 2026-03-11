# KoL Adaptive Lighting — Windows Build & Packaging Guide

## Overview

This document describes how to build a standalone Windows executable from the
KoL source code. The packaged application runs without requiring Python, pip,
VS Code, or any development tools on the end-user machine.

**Architecture**: PyInstaller one-directory bundle (`dist/KoL/`) with an
optional Inno Setup installer.

---

## Prerequisites (Developer Machine)

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.9+ | Runtime for build |
| pip | (bundled with Python) | Dependency installation |
| PyInstaller | 6.x | Freezes Python into exe |
| Inno Setup 6 | (optional) | Creates Setup wizard installer |

### Install Python Dependencies

```bat
pip install -r requirements.txt
pip install pyinstaller
```

---

## Quick Build

```bat
build_exe.bat
```

This will:
1. Install/upgrade dependencies and PyInstaller
2. Run `pyinstaller kol.spec --noconfirm`
3. Create the `data/` subdirectories in the output

**Output**: `dist/KoL/KoL.exe`

### Test the Build

```bat
cd dist\KoL
KoL.exe --dry-run
```

This starts the server in simulation mode (no USB hardware needed) and opens
`http://localhost:8080` in your browser.

---

## Project Structure (Packaging-Related Files)

```
KoL-v2/
├── launcher.py          # PyInstaller entry point (starts server + opens browser)
├── kol.spec             # PyInstaller spec file (defines what gets bundled)
├── build_exe.bat        # One-click build script
├── installer.iss        # Inno Setup installer script
├── requirements.txt     # Python dependencies
├── README-build.md      # This file
└── dalicontrol/
    ├── __init__.py      # Package marker (required by PyInstaller)
    ├── paths.py         # Centralized path resolution (dev vs frozen)
    ├── static/          # Web dashboard (bundled as read-only)
    │   ├── index.html
    │   ├── app.js
    │   └── style.css
    ├── main.py          # Application core
    ├── web_server.py    # FastAPI server
    └── ...              # Other modules
```

### How Path Resolution Works

`dalicontrol/paths.py` handles the difference between development and frozen
(PyInstaller) environments:

| Path | Development | Frozen (PyInstaller) |
|------|------------|---------------------|
| Static assets (read-only) | `dalicontrol/static/` | `_internal/dalicontrol/static/` |
| Telemetry CSVs | `dalicontrol/telemetry/` | `KoL/data/telemetry/` |
| ML models | `dalicontrol/models/` | `KoL/data/models/` |
| settings.json | `dalicontrol/settings.json` | `KoL/data/settings.json` |
| preferences.json | `dalicontrol/preferences.json` | `KoL/data/preferences.json` |
| state.json | `dalicontrol/state.json` | `KoL/data/state.json` |

Writable data lives in `data/` next to the exe, so it survives rebuilds and
upgrades.

---

## Command-Line Usage

```
KoL.exe [OPTIONS]

Options:
  --sensor-port PORT   Serial port for ESP32 (e.g. COM3). Required unless --dry-run.
  --sensor-baud RATE   Baud rate (default: 115200)
  --dry-run            Run without USB hardware (simulation mode)
  --mode {manual,ai}   Operating mode (default: manual)
  --web-port PORT      Dashboard port (default: 8080)
  --no-browser         Don't auto-open the browser
```

### Examples

```bat
REM Real hardware on COM3:
KoL.exe --sensor-port COM3

REM AI mode:
KoL.exe --sensor-port COM3 --mode ai

REM Simulation mode (no hardware):
KoL.exe --dry-run

REM Custom port:
KoL.exe --sensor-port COM3 --web-port 9090
```

---

## Building the Installer (Optional)

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
2. First run `build_exe.bat` to create `dist/KoL/`
3. Open `installer.iss` in Inno Setup Compiler
4. Update `#define MyAppVersion` if needed
5. Click **Build > Compile**

**Output**: `Output/KoL-Setup-{version}.exe`

The installer:
- Copies the application to Program Files
- Creates Start Menu shortcuts
- Optional desktop shortcut
- Creates writable `data/` directory with user permissions
- On uninstall, asks whether to keep user data

---

## Rebuilding After Code Changes

After modifying the Python source code:

```bat
REM From the repository root:
build_exe.bat
```

That's it. The spec file and build script handle everything. No need to
reconfigure anything unless you add new Python packages or data files.

### When to Update `kol.spec`

Update the spec file if you:
- **Add a new Python dependency**: Add to `hiddenimports` if PyInstaller
  doesn't auto-detect it (common with lazy imports)
- **Add new static assets**: Add to `datas` list
- **Add new dalicontrol modules**: Add to `hiddenimports`

---

## Versioning Convention

```
KoL-v{MAJOR}.{MINOR}.{PATCH}-build{YYYYMMDD}
```

- **MAJOR**: Breaking changes (hardware protocol, data format)
- **MINOR**: New features (new dashboard panels, new AI modes)
- **PATCH**: Bug fixes
- **build date**: Distinguishes builds from the same version

Examples:
- `KoL-v0.1.0-build20260311`
- `KoL-v1.0.0-build20260415`

Update the version in:
1. `installer.iss` — `#define MyAppVersion "0.1.0"`
2. Optionally tag the git commit: `git tag v0.1.0`

---

## Troubleshooting

### "Module not found" at runtime

A hidden import is missing. Add it to `hiddenimports` in `kol.spec` and
rebuild.

### Dashboard doesn't load / 404 errors

Static files not bundled correctly. Verify `datas` in `kol.spec` includes
the `dalicontrol/static` directory, and check that `STATIC_DIR` resolves
correctly by looking at the console log output.

### USB device not detected

- Check Device Manager for COM port assignment
- Ensure the ESP32 driver is installed (CP2102 or CH340)
- For the DALI controller: the `hidapi` DLL is bundled automatically by
  PyInstaller

### Console window closes immediately

Run from an existing command prompt to see error messages:
```bat
cd "C:\Program Files\KoL Adaptive Lighting"
KoL.exe --dry-run
```

---

## Test Checklist (Clean Windows Machine)

Use this checklist when verifying a build on a machine that has never had
Python or development tools installed.

- [ ] **Installation**: Run `KoL-Setup-{version}.exe` — installs without errors
- [ ] **Dry-run launch**: Run `KoL.exe --dry-run` — server starts, browser opens
- [ ] **Dashboard loads**: `http://localhost:8080` shows the dark-themed UI
- [ ] **Static assets**: CSS styling renders correctly, sliders and buttons visible
- [ ] **WebSocket**: Live status updates appear (sensor data refreshes every 5s)
- [ ] **Controls**: Brightness slider and CCT slider respond (dry-run: logged to console)
- [ ] **Settings panel**: Opens, saves, persists after restart
- [ ] **Preferences wizard**: Completes all 4 steps, saves to `data/preferences.json`
- [ ] **Telemetry**: After running for 30s+, a CSV appears in `data/telemetry/`
- [ ] **Telemetry charts**: Load a telemetry run in the dashboard — charts render
- [ ] **Mode switch**: Toggle Manual ↔ AI mode in the dashboard
- [ ] **Data persistence**: Stop and restart — settings, preferences, and state.json survive
- [ ] **Rebuild survives**: Re-run installer — user data in `data/` not overwritten
- [ ] **Hardware (if available)**: Connect ESP32 + DALI controller, run with `--sensor-port COM3`
- [ ] **Uninstall**: Uninstall via Windows — prompted about keeping data
