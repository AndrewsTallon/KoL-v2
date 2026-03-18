# KoL-v2 Security & Compliance Documentation

This document describes the security architecture, data flows, authentication mechanisms, and compliance considerations for the KoL-v2 Adaptive Lighting system.

---

## Table of Contents

1. [Security Architecture Overview](#security-architecture-overview)
2. [Authentication & Authorization](#authentication--authorization)
3. [Data Flow Diagram](#data-flow-diagram)
4. [Data Classification](#data-classification)
5. [Network Security](#network-security)
6. [API Security](#api-security)
7. [Data Storage & Retention](#data-storage--retention)
8. [Third-Party Dependencies](#third-party-dependencies)
9. [Deployment Hardening Checklist](#deployment-hardening-checklist)
10. [Threat Model](#threat-model)
11. [Incident Response](#incident-response)

---

## Security Architecture Overview

KoL-v2 is a **locally deployed** smart lighting system. By default, all components run on a single machine with no cloud dependencies. The system does not transmit data externally unless explicitly configured (e.g., weather API).

### Security Boundaries

```
┌─────────────────────────────────────────────────────────┐
│                   Local Machine                         │
│                                                         │
│  ┌──────────┐   USB Serial   ┌──────────────────────┐  │
│  │  ESP32    │──────────────►│  Python Backend       │  │
│  │  Sensor   │  (read-only)  │  (FastAPI + ML)       │  │
│  └──────────┘                │                        │  │
│                              │  ┌── Web Server ─────┐ │  │
│  ┌──────────┐   USB HID     │  │ localhost:8080     │ │  │
│  │  DALI     │◄─────────────│  │ API + WebSocket    │ │  │
│  │  Lamp     │  (write-only) │  └───────────────────┘ │  │
│  └──────────┘                └──────────────────────┘  │
│                                       │                 │
│                              ┌────────▼─────────┐      │
│                              │  Browser Client   │      │
│                              │  (Dashboard UI)   │      │
│                              └──────────────────┘      │
└─────────────────────────────────────────────────────────┘
         │                              │
         │ (optional)                   │ (optional)
         ▼                              ▼
  ┌──────────────┐             ┌───────────────────┐
  │ OpenWeatherMap│             │ cdn.jsdelivr.net  │
  │ API (weather)│             │ (Chart.js library)│
  └──────────────┘             └───────────────────┘
```

**Key principle**: The system is designed to operate entirely on the local network. External connections are optional and limited to weather data retrieval and frontend charting library loading.

---

## Authentication & Authorization

### API Key Authentication

The system supports API key authentication for all REST and WebSocket endpoints.

| Setting | Description |
|---------|-------------|
| Environment variable | `KOL_API_KEY` |
| Header | `X-API-Key: <your-key>` |
| WebSocket | `?token=<your-key>` query parameter |
| Default state | **Disabled** (development mode) |

#### Enabling Authentication

```bash
# Generate a secure random key
export KOL_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# Start the server — API key auth is now enforced
python -m dalicontrol.main --sensor-port /dev/ttyUSB0 --web
```

When `KOL_API_KEY` is set:
- All `/api/*` endpoints require the `X-API-Key` header
- WebSocket connections require the `?token=` query parameter
- Static assets (`/`, `/static/*`) and API docs (`/api/docs`, `/openapi*`) are exempt
- Invalid or missing keys return HTTP 401

When `KOL_API_KEY` is **not** set:
- A warning is logged: `API key authentication DISABLED`
- All endpoints are open (suitable for isolated development only)

#### Key Comparison Security

API key comparison uses `secrets.compare_digest()` to prevent timing-based side-channel attacks.

### Exempt Paths

These paths are accessible without authentication (even when API key is enabled):

| Path | Reason |
|------|--------|
| `/` | Dashboard HTML page |
| `/static/*` | CSS, JS, and other static assets |
| `/api/docs` | Interactive API documentation (Swagger UI) |
| `/openapi*` | OpenAPI schema |

---

## Data Flow Diagram

### Inbound Data Flows

```
ESP32 Sensor ──USB Serial──► usb_occupancy.py
  Sends: occupancy, lux, motion, signal strength
  Rate: ~1 Hz (configurable)
  Protocol: JSON over serial (115200 baud)
  Direction: Read-only (Python never writes to sensor)
```

### Internal Data Flows

```
Sensor Reader ──snapshot()──► Telemetry Logger ──write──► CSV files
                         └──► Adaptive Engine ──predict──► Lamp commands
                         └──► WebSocket ──push──► Browser dashboard
                         └──► Decision Log ──record──► In-memory buffer
```

### Outbound Data Flows

```
DALI Controller ◄──USB HID──── Lamp State Manager
  Sends: brightness level, color temperature (DTR/DTR1)
  Protocol: DALI DT8 via USB HID
  Direction: Write-only

OpenWeatherMap ◄──HTTPS GET──── Adaptive Engine (OPTIONAL)
  Sends: city name or lat/lon coordinates
  Receives: current weather conditions
  Triggered: Every 30 minutes (when AI mode active)
  Requires: weather_api_key setting configured

cdn.jsdelivr.net ◄──HTTPS GET──── Browser (OPTIONAL)
  Fetches: Chart.js library for telemetry visualization
  Triggered: On dashboard page load
  Note: Can be self-hosted to eliminate external dependency
```

### Data at Rest

| Data | Location | Format | Contains PII? |
|------|----------|--------|---------------|
| Telemetry logs | `telemetry/run_*.csv` | CSV | No (occupancy is boolean, no identity data) |
| ML models | `models/*.joblib` | Joblib (pickle) | No (statistical model weights only) |
| Settings | `settings.json` | JSON | Possibly (weather API key, location) |
| Preferences | `preferences.json` | JSON | No (schedule times, brightness preferences) |
| Lamp state | `state.json` | JSON | No (brightness level, color temp) |

---

## Data Classification

### No Personal Identifiable Information (PII) Collected

The system does **not** collect, store, or transmit:
- Names, email addresses, or account credentials
- IP addresses of clients (no access logging by default)
- Camera images or video
- Audio recordings
- Biometric data

### Occupancy Data

The mmWave radar sensor detects **presence** (boolean: occupied/vacant) and **motion** (boolean: moving/stationary). It does not:
- Identify individuals
- Count the number of people
- Track movement patterns beyond the desk area
- Capture any image or biometric data

Occupancy data is stored as boolean flags in telemetry CSVs and is used solely for lighting automation decisions.

### Sensitive Configuration Data

| Data | Sensitivity | Storage | Protection |
|------|------------|---------|------------|
| `KOL_API_KEY` | High | Environment variable only | Never written to disk by the application |
| `weather_api_key` | Medium | `settings.json` | Stored in plaintext; restrict file permissions |
| `weather_location` | Low | `settings.json` | City name or coordinates |
| `OPENAI_API_KEY` | High | Environment variable only | Never written to disk by the application |

---

## Network Security

### HTTP Security Headers

All HTTP responses include the following security headers (enforced by `_SecurityHeadersMiddleware`):

| Header | Value | Purpose |
|--------|-------|---------|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-type sniffing |
| `X-Frame-Options` | `DENY` | Prevents clickjacking via iframes |
| `X-XSS-Protection` | `1; mode=block` | Enables browser XSS filter |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Limits referrer information leakage |
| `Content-Security-Policy` | See below | Restricts resource loading origins |

### Content Security Policy (CSP)

```
default-src 'self';
script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
connect-src 'self' ws: wss:
```

| Directive | Allowed Sources | Justification |
|-----------|----------------|---------------|
| `default-src` | `'self'` | Only load resources from same origin |
| `script-src` | `'self'`, `'unsafe-inline'`, `cdn.jsdelivr.net` | Chart.js loaded from CDN; inline scripts for dashboard |
| `style-src` | `'self'`, `'unsafe-inline'` | Dashboard styles |
| `img-src` | `'self'`, `data:` | Local images and data URIs |
| `connect-src` | `'self'`, `ws:`, `wss:` | REST API and WebSocket connections |

### Binding Address

By default, the server binds to `127.0.0.1` (localhost only). To expose on the network:

```bash
# Binds to all interfaces — ensure API key is set!
python -m dalicontrol.main --web --web-host 0.0.0.0
```

**Recommendation**: Always set `KOL_API_KEY` when binding to non-localhost addresses.

---

## API Security

### Input Validation

All API endpoints use **Pydantic models** for request validation:

- `BrightnessRequest`: validates `pct` as float
- `CCTRequest`: validates `kelvin` as integer
- `ModeRequest`: validates `mode` against allowed values (`"manual"`, `"ai"`)
- `SettingsRequest`: validates all setting fields with proper types
- Telemetry file access uses `Path().name` to prevent path traversal attacks

### Path Traversal Prevention

Telemetry download endpoints sanitize filenames:
```python
safe_name = Path(filename).name  # Strips directory components
csv_path = TELEM_DIR / safe_name
```

### XSS Prevention

The dashboard JavaScript uses `textContent` (not `innerHTML`) for dynamic content, and the `escapeHtml()` function is applied to decision log entries that may contain user-generated text.

### Rate Limiting

No built-in rate limiting is implemented. For production deployments exposed to a network, place behind a reverse proxy (nginx, Caddy) with rate limiting configured.

---

## Data Storage & Retention

### Telemetry Files

- **Location**: `dalicontrol/telemetry/` (dev) or `data/telemetry/` (packaged)
- **Format**: CSV with headers
- **Naming**: `run_YYYYMMDD_HHMMSS_{mode}.csv`
- **Retention**: No automatic deletion; files accumulate until manually removed
- **Size**: ~100-200 KB per hour of operation

### Retention Recommendations

| Environment | Recommendation |
|-------------|---------------|
| Research | Keep all telemetry for analysis |
| Office deployment | Rotate weekly or monthly; archive older files |
| Compliance-sensitive | Define retention policy; automate deletion with cron job |

### Data Deletion

To completely remove all collected data:

```bash
# Remove telemetry data
rm -rf dalicontrol/telemetry/run_*.csv

# Remove trained models
rm -rf dalicontrol/models/*.joblib

# Remove user preferences and settings
rm -f dalicontrol/preferences.json dalicontrol/settings.json

# Remove lamp state
rm -f dalicontrol/state.json
```

---

## Third-Party Dependencies

### Python Packages

| Package | Purpose | License | Network Access |
|---------|---------|---------|---------------|
| `fastapi` | Web framework | MIT | No (serves locally) |
| `uvicorn` | ASGI server | BSD-3 | No (binds locally) |
| `pydantic` | Data validation | MIT | No |
| `scikit-learn` | ML models (RandomForest) | BSD-3 | No |
| `joblib` | Model serialization | BSD-3 | No |
| `pyserial` | ESP32 serial communication | BSD-3 | No |
| `hidapi` | USB HID for DALI controller | BSD-3 | No |
| `openai` | Natural language commands (optional) | MIT | Yes (OpenAI API) |

### Frontend Dependencies

| Library | Source | Purpose | Integrity |
|---------|--------|---------|-----------|
| Chart.js 4.x | `cdn.jsdelivr.net` | Telemetry charts | Loaded via HTTPS; CSP-restricted |

**Self-hosting option**: Download Chart.js to `dalicontrol/static/` and update `index.html` to eliminate the CDN dependency entirely.

### Supply Chain Considerations

- All Python dependencies are installed via `pip` from PyPI
- Pin exact versions in `requirements.txt` for reproducible builds
- No post-install scripts or native compilation required (except `hidapi`)
- Frontend has a single external dependency (Chart.js) loaded from a trusted CDN

---

## Deployment Hardening Checklist

Use this checklist before deploying KoL-v2 in a production or shared environment:

- [ ] **Set API key**: `export KOL_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")`
- [ ] **Verify auth is active**: Check server log for `API key authentication enabled`
- [ ] **Restrict binding**: Only bind to `0.0.0.0` if network access is required
- [ ] **Use HTTPS**: Place behind a reverse proxy (nginx/Caddy) with TLS termination
- [ ] **Restrict file permissions**: `chmod 600 dalicontrol/settings.json` (contains weather API key)
- [ ] **Set up log rotation**: Configure logrotate for application logs
- [ ] **Define data retention**: Implement automated telemetry file cleanup
- [ ] **Review firewall rules**: Only expose the web port (default 8080) if needed
- [ ] **Pin dependencies**: Use exact versions in `requirements.txt`
- [ ] **Disable API docs in production**: Consider removing `/api/docs` endpoint access
- [ ] **Self-host Chart.js**: Download to `static/` to eliminate CDN dependency
- [ ] **Monitor disk usage**: Telemetry CSVs grow continuously; set up alerts

---

## Threat Model

### Attack Surface

| Surface | Risk | Mitigation |
|---------|------|------------|
| Web API (unauthenticated) | Unauthorized lamp control | Set `KOL_API_KEY` |
| WebSocket | Unauthorized real-time data access | API key token validation |
| USB Serial (ESP32) | Malicious serial data injection | Input parsing with error handling; read-only |
| USB HID (DALI) | Unauthorized lamp commands | Physical access required; no network exposure |
| Telemetry CSVs | Data exfiltration | File system permissions; no PII in data |
| Settings file | API key leakage (weather key) | File permissions; sensitive keys in env vars |
| CDN dependency | Supply chain attack via Chart.js | CSP restricts to `cdn.jsdelivr.net`; self-host option |

### OWASP Top 10 Considerations

| Category | Status | Notes |
|----------|--------|-------|
| A01 Broken Access Control | Mitigated | API key auth; path traversal prevention |
| A02 Cryptographic Failures | N/A | No encryption needed (local-only data, no PII) |
| A03 Injection | Mitigated | Pydantic validation; parameterized queries; no SQL |
| A04 Insecure Design | Mitigated | Defense-in-depth; security headers; CSP |
| A05 Security Misconfiguration | Warning shown | Auth-disabled warning in logs |
| A06 Vulnerable Components | Monitor | Pin dependencies; update regularly |
| A07 Auth Failures | Mitigated | Timing-safe comparison; env-var-based keys |
| A08 Data Integrity Failures | Low risk | No deserialization of untrusted data (except joblib models) |
| A09 Logging Failures | Mitigated | Actions logged to CSV and console |
| A10 SSRF | N/A | Only outbound call is weather API with user-configured URL |

---

## Incident Response

### If API key is compromised

1. Generate a new key: `export KOL_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")`
2. Restart the server
3. Update any clients using the old key

### If telemetry data is exposed

1. Assess scope: telemetry contains no PII (occupancy is boolean only)
2. Review access logs if reverse proxy is configured
3. Rotate weather API key if `settings.json` was exposed

### If weather API key is compromised

1. Regenerate the key at OpenWeatherMap
2. Update `settings.json` via the dashboard or direct file edit
3. Restart the server
