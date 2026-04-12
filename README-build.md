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
3. Create the `data/` subdirectories in the output, including `data/profiles`

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
| Participant profiles | `dalicontrol/profiles/` | `KoL/data/profiles/` |
| profiles.json | `dalicontrol/profiles.json` | `KoL/data/profiles.json` |
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
2. **Stage the CP210x USB driver** (see next section) so that end-user PCs
   can talk to the ESP32 sensor out of the box
3. Run `build_exe.bat` to create `dist/KoL/`
4. Open `installer.iss` in Inno Setup Compiler
5. Update `#define MyAppVersion` if needed
6. Click **Build > Compile**

**Output**: `Output/KoL-Setup-{version}.exe`

The installer:
- Copies the application to Program Files
- Silently installs the CP210x USB-to-UART driver via `pnputil`
  (only if `drivers/cp210x/silabser.inf` is present at build time)
- Creates Start Menu shortcuts
- Optional desktop shortcut
- Creates writable `data/`, `data/telemetry`, `data/models`, and `data/profiles`
  directories with user permissions
- On uninstall, asks whether to keep user data

### Bundling the CP210x USB Driver

The ESP32 sensor speaks to the PC through a Silicon Labs CP2102 USB-to-UART
bridge. On a Windows PC that has never had the driver installed, the sensor
appears in Device Manager as an unknown device ("Code 28 — The drivers for
this device are not installed") and **no COM port is ever created**, so the
app cannot see the sensor.

To make the installer handle this automatically:

1. Download the **CP210x Universal Windows Driver** package from Silicon
   Labs (search for "CP210x USB to UART Bridge VCP Drivers" on
   silabs.com). It ships as a ZIP (`CP210x_Universal_Windows_Driver.zip`).
2. Extract it into `drivers/cp210x/` in the repo so that
   `drivers/cp210x/silabser.inf` exists.
3. Run `build_exe.bat` — it prints `[OK] CP210x driver found` when the INF
   is detected.
4. Compile `installer.iss` as usual.

See `drivers/cp210x/README.txt` for the full checklist. The driver files
themselves are gitignored (they're a third-party redistributable owned by
Silicon Labs); only the README is tracked.

At install time on the target PC, Inno Setup (running as admin) executes:

```
pnputil /add-driver drivers\cp210x\silabser.inf /install
```

This stages the driver into the Windows Driver Store and installs it for
any currently-connected CP2102 device. `pnputil` ships with Windows 7+, so
no additional redistributable is required. The operation is idempotent —
re-running the installer on a machine that already has the driver is a
no-op.

If you omit step 1-3, the installer still compiles, but it skips the
driver-install step (guarded by an Inno Setup `Check` function) and you'll
need some other mechanism to get the driver onto target PCs.

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

- **First**: check Device Manager. If the ESP32 appears under
  **"Other devices"** as `CP2102 USB to UART Bridge Controller` with a
  yellow ⚠ and "Code 28", the CP210x driver is missing — see the
  "Bundling the CP210x USB Driver" section above. The packaged installer
  will drop it automatically if you staged `drivers/cp210x/` before
  building.
- If nothing at all appears in Device Manager when you plug the cable in,
  the cable is likely **power-only** (common with cheap micro-USB
  cables) — swap to a data-capable cable.
- If the ESP32 enumerates under **"Ports (COM & LPT)"** (e.g.
  `Silicon Labs CP210x USB to UART Bridge (COM3)`) but the app still
  can't see it, check whether another program (Arduino IDE Serial
  Monitor, PuTTY, etc.) is holding the port open.
- For the DALI controller: the `hidapi` DLL is bundled automatically by
  PyInstaller.

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
- [ ] **Profile gate**: Dashboard opens with the blocking profile picker before controls are used
- [ ] **Initial participant info**: Create a profile and confirm `data/profiles/{profile_id}/participant_info.json` is written
- [ ] **API keys onboarding**: Enter OpenAI/weather keys during profile creation and confirm they persist in `data/settings.json` after restart
- [ ] **Profile model path**: Train AI models and confirm model files are written under `data/profiles/{profile_id}/models/`
- [ ] **Final evaluation**: Use **Answer Final Evaluation** and confirm a timestamped JSON appears in `data/profiles/{profile_id}/final_evaluations/`
- [ ] **Static assets**: CSS styling renders correctly, sliders and buttons visible
- [ ] **WebSocket**: Live status updates appear (sensor data refreshes every 5s)
- [ ] **Controls**: Brightness slider and CCT slider respond (dry-run: logged to console)
- [ ] **Settings panel**: Opens, saves, persists after restart
- [ ] **Telemetry**: After running for 30s+, a CSV appears in `data/telemetry/`
- [ ] **Telemetry profile tags**: New telemetry rows include `profile_id` and `profile_name`
- [ ] **Telemetry charts**: Load a telemetry run in the dashboard — charts render
- [ ] **Mode switch**: Toggle Manual ↔ AI mode in the dashboard
- [ ] **Data persistence**: Stop and restart — active profile, final evaluation files, settings, and state.json survive
- [ ] **Rebuild survives**: Re-run installer — user data in `data/`, especially `data/profiles`, is not overwritten
- [ ] **Hardware (if available)**: Connect ESP32 + DALI controller, run with `--sensor-port COM3`
- [ ] **Uninstall**: Uninstall via Windows — prompted about keeping data
