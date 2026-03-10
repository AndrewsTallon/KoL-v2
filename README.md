# KoL-v2: AI-Powered Smart Lighting Control

A research-grade adaptive lighting system that learns your preferences and automates desk lighting for comfort and energy savings. Built on DALI protocol with ESP32 occupancy sensing and machine-learning control.

---

## Executive Summary

KoL-v2 (Kingdom of Light v2) is a smart desk lighting system designed for both research and practical deployment. It solves a common workplace problem: desk lamps that are either too bright, too dim, the wrong color temperature, or left on when nobody is around.

The system uses a small radar sensor to detect whether someone is sitting at the desk, along with an ambient light sensor that measures how bright the room already is. During an initial **baseline phase**, it collects data about how you prefer your lighting throughout the day. Then, using machine learning, it builds a personalized lighting profile that automatically adjusts brightness and color temperature based on the time of day and ambient conditions — warmer light in the evening for relaxation, cooler light at midday for focus.

Every automated decision includes a **human-readable rationale** — a brief explanation of *why* the system made each adjustment, displayed in the web dashboard and saved in the telemetry data. This makes the system transparent and auditable, which is critical for research validation and user trust.

The result: a desk lamp that adapts to you, saves energy by turning off when you leave, and provides full data transparency for analysis.

---

## System Architecture

```
┌────────────────────┐         USB Serial (115200 baud)
│   ESP32 Sensor     │ ──────────────────────────────────┐
│  LD2410 Radar      │   JSON telemetry every 1s         │
│  BH1750 Light      │   occupancy + lux + signal data   │
└────────────────────┘                                    │
                                                          ▼
                                              ┌───────────────────────┐
                                              │   Python Backend      │
                                              │                       │
                                              │  ┌─ Sensor Reader ──┐ │
                                              │  │ usb_occupancy.py │ │
                                              │  └────────┬─────────┘ │
                                              │           │           │
                                              │  ┌────────▼─────────┐ │
                                              │  │ Telemetry Logger │ │    CSV files
                                              │  │   (main.py)      │───────────────►  telemetry/
                                              │  └────────┬─────────┘ │
                                              │           │           │
                                              │  ┌────────▼─────────┐ │
                                              │  │ Control Engine   │ │
                                              │  │  Baseline: rules │ │
                                              │  │  AI: RandomForest│ │
                                              │  └────────┬─────────┘ │
                                              │           │           │
                                              │  ┌────────▼─────────┐ │    USB HID
                                              │  │ DALI Controller  │──────────────►  DALI Luminaire
                                              │  │ dali_controls.py │ │
                                              │  └──────────────────┘ │
                                              │                       │
                                              │  ┌──────────────────┐ │
                                              │  │ Web Dashboard    │ │    http://localhost:8080
                                              │  │ FastAPI + WS     │───────────────►  Browser
                                              │  └──────────────────┘ │
                                              └───────────────────────┘
```

---

## How It Works

### 1. Sensing (ESP32 Firmware)

The ESP32 microcontroller runs custom firmware that combines two sensors:

- **LD2410 mmWave Radar** — Detects human presence through millimeter-wave radar. Can distinguish a person sitting at a desk from background noise like PC fans or RGB lighting. Uses three layers of filtering:
  - **Signal-strength gating** — Rejects weak reflections from fans and electronics
  - **Confirmation window** — Requires 3 of 5 consecutive readings to agree before changing state
  - **Time debounce** — 800ms delay before turning on, 8s delay before turning off

- **BH1750 Light Sensor** — Measures ambient illuminance (lux) with exponential moving average smoothing to eliminate flicker.

The sensor sends JSON telemetry over USB serial at configurable rates (default 1 Hz), including occupancy status, signal strength, distances, ambient light, and diagnostic data.

### 2. Baseline Mode (Data Collection)

In baseline mode, the system records how you use your desk lamp throughout the day:

- **When you arrive**: Light turns on at 75% brightness
- **When you leave**: Light dims to 10% as a warning, then turns off after 60 seconds
- **All your manual adjustments** (brightness, color temperature) are logged with timestamps

This creates a rich dataset of your lighting preferences correlated with time-of-day and ambient conditions.

### 3. AI Adaptive Mode (Learned Control)

After collecting baseline data, the system trains **RandomForest** machine learning models that predict your preferred brightness and color temperature based on:

- **Time of day** (encoded cyclically as sin/cos to handle midnight wraparound)
- **Ambient light level** (lux from the BH1750 sensor)

Every 5 minutes (when occupied), the AI evaluates whether to adjust the lighting. It applies **threshold filters** (minimum 5% brightness change, 100K color temperature change) to prevent annoying micro-adjustments.

**Fallback heuristics** are built in if ML models aren't available:
- Brightness follows an inverse daylight curve (more sunlight → less artificial light)
- Color temperature follows a circadian rhythm (warm morning/evening, cool midday)

### 4. Decision Rationale

Every automated action includes a human-readable explanation:

| Decision | Rationale Example |
|----------|-------------------|
| AI brightness adjustment | "Bright ambient light (420 lux) -> brightness 30%" |
| AI color temperature | "Midday (12.5h) -> cool white 5800K" |
| Occupancy restore | "Person detected at desk -> restoring light to 75%" |
| Vacancy dimming | "Desk vacant -> dimming to 10% as warning before shutdown" |
| Energy shutdown | "Vacant for 60s after dimming -> turning off to save energy" |

These rationales are displayed live in the web dashboard's **Decision Log** panel and saved in the telemetry CSV for post-hoc analysis.

---

## Hardware Requirements

| Component | Model | Purpose |
|-----------|-------|---------|
| Microcontroller | ESP32 (any DevKit) | Sensor hub, serial telemetry |
| Radar sensor | HiLink LD2410 | Occupancy detection (mmWave) |
| Light sensor | BH1750 | Ambient illuminance (lux) |
| DALI controller | USB-DALI interface (VID 0x17B5, PID 0x0020) | Lamp communication |
| Luminaire | Any DALI DT8 tunable white | Desk lamp (2700K–6500K) |

### ESP32 Wiring

| ESP32 Pin | Connected To |
|-----------|-------------|
| GPIO 26 (SDA) | BH1750 SDA |
| GPIO 32 (SCL) | BH1750 SCL |
| GPIO 33 (RX) | LD2410 TX |
| GPIO 27 (TX) | LD2410 RX |
| 3.3V | BH1750 VCC, LD2410 VCC |
| GND | BH1750 GND, LD2410 GND |

---

## Software Requirements

- **Python 3.9+**
- **Arduino IDE** or **PlatformIO** (for ESP32 firmware)
- **Operating System**: Linux recommended (USB HID + serial access); macOS also supported

### Python Dependencies

```
openai              # Optional: LLM-powered natural language commands
hidapi              # USB HID transport for DALI controller
pyserial            # Serial communication with ESP32
fastapi             # Web server framework
uvicorn[standard]   # ASGI server for FastAPI
scikit-learn        # Machine learning models (RandomForest)
joblib              # Model persistence
```

---

## Quick Start

### 1. Flash the ESP32 Firmware

Open `current_arduino,txt` in Arduino IDE or PlatformIO. Install the required libraries:
- `BH1750` by Christopher Laws
- `MyLD2410` (included or available via library manager)

Flash to your ESP32. The sensor will begin outputting JSON on the USB serial port.

### 2. Install Python Dependencies

```bash
# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install all dependencies
pip install -r requirements.txt
```

### 3. Set Up Permissions (Linux)

```bash
# USB serial access (ESP32)
sudo usermod -aG dialout $USER

# USB HID access (DALI controller)
sudo tee /etc/udev/rules.d/99-dali.rules << 'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="17b5", ATTRS{idProduct}=="0020", MODE="0666"
EOF
sudo udevadm control --reload-rules

# Log out and back in for group changes to take effect
```

### 4. Run Baseline Data Collection

```bash
python -m dalicontrol.main \
  --sensor-port /dev/ttyUSB0 \
  --auto \
  --mode baseline \
  --web \
  --web-port 8080
```

Use your desk lamp normally for 1–3 days. The system logs all occupancy patterns and your manual brightness/CCT adjustments. Open `http://localhost:8080` to monitor in real time.

### 5. Train AI Models

From the web dashboard, click **"Train Models from Baseline"** in the AI panel. Or switch to AI mode and models will train automatically from collected baseline data.

### 6. Run in AI Adaptive Mode

```bash
python -m dalicontrol.main \
  --sensor-port /dev/ttyUSB0 \
  --auto \
  --mode ai \
  --web \
  --web-port 8080
```

The system now automatically adjusts your lighting based on learned preferences. Monitor decisions in the **Decision Log** panel.

---

## All CLI Options

```bash
python -m dalicontrol.main [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--sensor-port PORT` | *(required)* | Serial port for ESP32 sensor (e.g., `/dev/ttyUSB0`) |
| `--sensor-baud RATE` | `115200` | Baud rate for sensor serial communication |
| `--auto` | off | Enable automatic occupancy-based lamp control |
| `--mode {baseline,ai}` | `baseline` | Operating mode: `baseline` for data collection, `ai` for adaptive control |
| `--web` | off | Start the web dashboard server |
| `--web-port PORT` | `8080` | Port for the web dashboard |
| `--no-cli` | off | Disable the CLI input loop (useful with `--web`) |
| `--nominal-power WATTS` | `40` | Luminaire power rating for energy estimation |
| `--dry-run` | off | Simulate all hardware — no DALI commands sent, no sensor required |

### Common Command Combinations

```bash
# Quick test — no hardware needed
python -m dalicontrol.main --dry-run --sensor-port /dev/null --web

# Baseline collection with dashboard
python -m dalicontrol.main --sensor-port /dev/ttyUSB0 --auto --mode baseline --web --web-port 8080

# AI mode, web only (no CLI prompts)
python -m dalicontrol.main --sensor-port /dev/ttyUSB0 --auto --mode ai --web --web-port 8080 --no-cli

# CLI only, no web server
python -m dalicontrol.main --sensor-port /dev/ttyUSB0 --auto --mode baseline
```

---

## Web Dashboard

The web dashboard at `http://localhost:8080` provides:

- **Live Status** — Real-time lamp brightness, CCT, occupancy, and ambient light
- **Manual Controls** — Sliders for brightness (0–100%) and CCT (2700–6500K), ON/OFF buttons
- **Telemetry Charts** — Time-series plots of brightness vs. lux, color temperature, and occupancy
- **Decision Log** — Scrollable list of the last 50 automated decisions with human-readable rationale
- **AI Controls** — Model training trigger (visible in AI mode)
- **Data Export** — Download telemetry CSVs for external analysis

The dashboard updates via WebSocket (5-second intervals) with automatic reconnection.

---

## Data & Telemetry

### CSV Files

Telemetry is logged to `dalicontrol/telemetry/` as CSV files named `run_YYYYMMDD_HHMMSS_{mode}.csv`.

**Columns:**

| Column | Type | Description |
|--------|------|-------------|
| `ts_epoch` | float | Unix timestamp |
| `ts_iso` | string | ISO 8601 timestamp |
| `mode` | string | "baseline" or "ai" |
| `raw_present` | bool | Raw radar detection (before filtering) |
| `filt_occupied` | bool | Filtered occupancy (after 3-layer filter) |
| `moving` | bool | Motion detected by radar |
| `stationary` | bool | Stationary target detected |
| `lux` | float | Raw ambient light (lux) |
| `lux_smooth` | float | EMA-smoothed lux from ESP32 |
| `moving_age_ms` | int | Milliseconds since last motion |
| `moving_events` | int | Cumulative motion event count |
| `sensor_age_s` | float | Time since last sensor reading |
| `move_dist` | int | Moving target distance (cm) |
| `move_energy` | int | Moving target signal strength (0–100) |
| `still_dist` | int | Stationary target distance (cm) |
| `still_energy` | int | Stationary target signal strength (0–100) |
| `sensor_seq` | int | ESP32 heartbeat sequence number |
| `confirm_count` | int | Confirmation window count (0–5) |
| `filter_stage` | string | Active filter: "instant", "confirmed", "debounced" |
| `lamp_is_off` | bool | Lamp power state |
| `lamp_level` | int | DALI brightness level (0–254) |
| `lamp_temp_dtr` | int | DALI DTR register (color temp) |
| `lamp_temp_dtr1` | int | DALI DTR1 register (color temp) |
| `cct_kelvin` | int | Color temperature in Kelvin |
| `runtime_s` | float | Total lamp-on runtime (seconds) |
| `action` | string | Command executed (e.g., "set_brightness_pct(75)") |
| `reason` | string | Machine-readable reason code |
| `rationale` | string | Human-readable decision explanation |
| `user_text` | string | Natural language command (if user-initiated) |

### Logging Frequency

- **Heartbeat**: Every 5 seconds (sensor + lamp state snapshot)
- **Event-driven**: Immediately on any action (occupancy change, AI adjustment, user command)

---

## Project Structure

```
KoL-v2/
├── README.md                          # This file
├── requirements.txt                   # Python dependencies
├── current_arduino,txt                # ESP32 firmware source code
├── dalicontrol/
│   ├── main.py                        # Entry point, telemetry logger, orchestrator
│   ├── ai_operator.py                 # Natural language command parser (LLM + rules)
│   ├── adaptive_engine.py             # ML adaptive control (RandomForest)
│   ├── web_server.py                  # FastAPI REST API + WebSocket server
│   ├── lamp_state.py                  # Lamp state management + DALI abstraction
│   ├── dali_controls.py               # Low-level DALI DT8 protocol commands
│   ├── dali_transport.py              # USB HID transport for DALI
│   ├── usb_occupancy.py               # ESP32 serial reader + occupancy parsing
│   ├── cct_utils.py                   # Kelvin ↔ DTR/DTR1 conversion utilities
│   ├── energy_estimator.py            # Energy consumption analysis
│   ├── state.json                     # Persistent lamp state (auto-saved)
│   ├── static/
│   │   ├── index.html                 # Web dashboard HTML
│   │   ├── app.js                     # Dashboard JavaScript (charts, WebSocket)
│   │   └── style.css                  # Dashboard styling (dark theme)
│   ├── telemetry/                     # CSV telemetry data files
│   │   └── run_*.csv
│   └── models/                        # Trained ML models (.joblib)
│       ├── brightness_model.joblib
│       └── cct_model.joblib
```

---

## Troubleshooting

### ESP32 sensor not detected

```
USB error (could not open port /dev/ttyUSB0). Reconnecting in 2s...
```

- Check the USB cable is connected and the ESP32 is powered
- Verify the port: `ls /dev/ttyUSB*` or `ls /dev/ttyACM*`
- Check permissions: `sudo usermod -aG dialout $USER` then log out/in
- Try the port explicitly: `--sensor-port /dev/ttyACM0`

### DALI controller not responding

```
HID open failed
```

- Check that the DALI USB controller is plugged in
- Verify udev rules are installed (see Setup section)
- Run `lsusb | grep 17b5` to confirm the device is detected
- Try `--dry-run` to verify the rest of the system works without hardware

### No occupancy detection / false triggers

- Check the radar range: the default is 1.5m (gate 2). Ensure the sensor faces the desk area
- Review signal strength in the telemetry CSV (`move_energy`, `still_energy`). A seated person typically reads 30–90; false triggers from fans read 5–20
- The `confirm_count` and `filter_stage` fields in telemetry help diagnose which filter layer is triggering

### AI models not training

```
Insufficient training data (N samples). Need at least 10.
```

- You need at least 10 samples where the lamp is ON and someone is present
- Run in baseline mode for longer, making manual brightness/CCT adjustments
- Check that baseline CSV files exist: `ls dalicontrol/telemetry/run_*_baseline.csv`

### Web dashboard not loading

- Confirm `--web` flag is set when launching
- Check the port isn't already in use: `lsof -i :8080`
- Try a different port: `--web-port 9090`

---

## Natural Language Commands

When running with CLI input enabled, type natural language commands:

```
you> set to 30% and warm
you> make it cool and max brightness
you> turn off
you> turn on
```

Commands are parsed using OpenAI function calling (if `OPENAI_API_KEY` is set) or a built-in rules-based parser as fallback.

```bash
# Optional: enable LLM-powered command parsing
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini   # optional, defaults to gpt-4o-mini
```
