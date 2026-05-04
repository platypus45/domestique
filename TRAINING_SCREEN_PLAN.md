# Live Training Screen + Native App Window

## Context
The app currently opens in a browser tab. The user wants:
1. **Native app window** (like Claude desktop) — not a browser tab
2. **Live training screen** with real-time data fields visible at arm's length during a ride
3. **TACX trainer control** via BLE — send CRS slopes, receive telemetry
4. **Elevation/workout tracking** — position on route, current interval block, W'bal gauge

GoldenCheetah is 500K+ lines of C++ — can't import. But **pycycling** (Python, last commit 7 days ago) already implements the exact same FTMS BLE protocol. The formulas (W'bal, NP, TSS, XPower) are standard math, ~50 lines each.

### Protocol Research Summary

**FTMS BLE (all modern TACX/Wahoo/Elite trainers):**
- Service UUID: `0x1826`
- Indoor Bike Data (`0x2AD2`): power (int16 W), speed (uint16 ÷ 100 km/h), cadence (uint16 ÷ 2 rpm), HR (uint8 bpm)
- Control Point (`0x2AD9`): `SET_TARGET_INCLINE` (0x03, int16 ÷ 0.1 = grade%), `SET_TARGET_POWER` (0x05, int16 W), `SET_INDOOR_BIKE_SIMULATION_PARAMS` (0x11, wind+grade+crr+cda)
- Handshake: enable indications → `REQUEST_CONTROL` (0x00) → wait for ACK → then send commands
- **pycycling handles all of this** including TACX proprietary BLE UART for NEO road-feel

**Dual BLE** (trainer + HR strap): bleak supports 2+ concurrent connections natively via asyncio.

### reference Patterns Found (from local prefs.xml + knowndevices.xml)
- **Per-role device persistence**: separate `LASTPOWERDEVICE`, `LASTCONTROLLABLETRAINER`, `LASTCADENCEDEVICE`, `LASTHRMDEVICE` — each role remembers its device independently
- **TRAINER_EFFECT: 0.5** — gradient difficulty scaling (50% = slopes halved). Add as "Trainer Difficulty" slider in our settings.
- **NEOROADFEEL: 1** — TACX Neo road feel (pycycling supports this via TACX proprietary BLE UART)
- **POWERSMOOTHING: 0** — raw vs 3s smoothed power toggle
- **knowndevices.xml** format: `[protocol] [bleId] [antId] deviceName` — persistent device memory across sessions
- **Per-device reconnection**: if one device drops, only that device reconnects (not all). Modular connection handlers.
- **Your hardware**: TACX Neo 2T (382036089, BLE UUID 12DF5F64-D91A-EB3E-7487-23821DBE22CC) + Garmin HRM-Pro+ (420717723, UUID 499EBE6C-6A83-836A-7573-5534A65A835E)
- **Critical: Neo 2T uses proprietary TACX BLE** (service 6E40FEC1, NOT standard FTMS 0x1826). Gradient commands write to 6E40FEC3. pycycling implements this exact protocol.
- **27 road surface types** in the reference data (Cobblestone 4 variants, DirtRoad, BrickRoad, Gravel, etc.) → map to pycycling's `set_neo_modes(road_surface_pattern=COBBLESTONES_HARD)` for road feel
- **GoldenCheetah does NOT have road feel** — we can add this as a differentiating feature
- **BLE connect time**: ~1 second total (scan → connect → service discovery → ready)
- **Gradient update rate**: writes to 6E40FEC3 every 1-2 seconds during ride

### UI Research Summary (what makes modern training screens great)

- **Power: 56pt bold**, dominant center — glanceable at 80cm trainer distance
- **Time remaining** top-right (where eyes naturally look)
- **Current vs target**: "+12W" / "-8W" deviation indicator, color-coded green/red
- **W'bal gauge**: vertical bar, green (>75%) → yellow (25-75%) → red (<25%, flashing)
- **Dark mode mandatory** for sweaty indoor sessions with high-contrast text
- **Workout blocks** as timeline with "now" marker advancing (TrainerRoad style)
- **Elevation profile** with position dot moving along (reference style)

---

## Phase 0: Multi-Profile System

**Goal**: Multiple athlete profiles so you and your partner each have your own FTP, zones, training plan, and ride history.

**New file:**
- **`profiles.py`** — `ProfileManager` class:
  - Each profile = a directory under `profiles/{name}/` containing its own `.env`, `config.py` overrides, `plan.json`, `user_prefs.json`, ride history
  - `create_profile(name)` → creates profile directory with defaults
  - `switch_profile(name)` → reloads config, clears cache, swaps DB connection
  - `list_profiles()` → returns available profiles
  - Active profile stored in `active_profile.json`

**Modified files:**
- `app.py` — add profile selector to header + `/api/profiles/*` endpoints
- `templates/dashboard.html` — profile switcher dropdown in top bar (next to theme toggle)
- `templates/setup.html` — Step 1 now asks "Create your profile" with name input
- `config.py` — loaded per-profile (each profile overrides athlete metrics)
- `db.py` — per-profile SQLite database OR profile_id column on all tables

**UI**: Profile avatar/icon in header → dropdown with "Martijn" / "Partner" / "Add profile"

---

## Phase 1: Native App Window (pywebview)

**Goal**: Replace browser tab with native OS window.

**Changes:**
- `launcher.py` — replace `webbrowser.open(URL)` with `webview.create_window("Health Tracker", URL, width=1400, height=900)` + `webview.start()`. Fallback to browser if pywebview not installed.
- `requirements.txt` — add `pywebview>=5.0`
- `health_tracker.spec` — add `pywebview` to hiddenimports

---

## Phase 2: Live Training Screen UI (mock data)

**Goal**: Full-screen ride display with all data fields, working with mock data first.

### New files:
- **`templates/training.html`** — full-screen training view (separate page, not in 3700-line dashboard.html)
- **`training_live.py`** — `TrainingSession` class: real-time state + derived metrics

### New routes in app.py:
- `GET /training` — serves training.html
- `WebSocket /ws/training` — 1Hz telemetry push
- `POST /api/training/start` — start session (mode + file)
- `POST /api/training/stop` — end session, save
- `POST /api/training/pause` — toggle pause

### Training Screen Layout (dark mode, arm's length readable):

```
┌─────────────────────────────────────────────────────────────────┐
│ TRAINING  Martijn ▼  [▶ PAUSE]  [■ STOP]  [+5%][−5%]  ⏱ 47:23│
│                                     INT 2/5 · 3:42 remaining   │
├────────────────────────┬────────────────────────────────────────┤
│                        │                                        │
│        ⚡ 245          │    TARGET  238W                        │
│         watts          │    ██████████████████░░  +7W           │
│        (56pt bold)     │    ▲ deviation bar (green = on target) │
│                        │                                        │
│   ❤️ 156    🔄 88      │    W'BAL                               │
│    bpm      rpm        │    ████████████░░░░░░  14.2 kJ (71%)  │
│                        │    ▲ vertical gauge, green→yellow→red  │
│   🚴 32.4   ⛰️ 4.2%    │                                        │
│    km/h     slope      │    NP 231W · IF 0.92 · TSS 67         │
│                        │                                        │
├────────────────────────┴────────────────────────────────────────┤
│ ROUTE MAP + ELEVATION                                           │
│ ┌──────────────────────┬────────────────────────────────────────┐│
│ │  Leaflet GPX map     │  ELEVATION PROFILE                    ││
│ │  with position dot   │  [▓▓▓▓●░░░░░░░░░░]  21.3/108km      ││
│ │  + gradient-colored  │  Current: 4.2%  Next 500m: 6.8%→8.1% ││
│ │  route polyline      │  Summit in: 3.2km                     ││
│ │  (auto-pans)         │  Road: Cobblestone ⬛⬛⬛░░            ││
│ └──────────────────────┴────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────┤
│ WORKOUT BLOCKS (if in workout/hybrid mode)                      │
│ [WU 10m][▓▓SS 20m▓▓▓●▓▓▓][REST 5m][SS 20m][REST 5m][SS 20m][CD│
│  Target: Sweet Spot 213-238W │ Next: 5min recovery @ 50%       │
└─────────────────────────────────────────────────────────────────┘

LIVE DATA FEED GRAPH (bottom strip, simulator-style):
┌─────────────────────────────────────────────────────────────────┐
│ ⚡ Power (yellow) ──── ❤️ HR (red) ──── 🔄 Cadence (blue)       │
│ ┌───────────────────────────────────────────────────────────┐   │
│ │     ╱╲    ╱╲╱╲                              ╱╲           │   │
│ │ ───╱──╲──╱────╲──────────────────────────╱╲╱──╲────── ⚡ │   │
│ │   ╱    ╲╱      ╲    ╱────────────────╲╱╱     ╲       │   │
│ │──────────────────────────────────────────────────── ❤️ │   │
│ │ ─────────────────────────────────────────────────── 🔄 │   │
│ └───────────────────────────────────────────────────────────┘   │
│  -5min              -3min              -1min              NOW   │
└─────────────────────────────────────────────────────────────────┘

- Scrolling 5-minute window, updates at 1Hz
- 3 overlaid lines: power (yellow, 3s smoothed), HR (red), cadence (blue, dashed)
- Y-axis auto-scales per metric; zone bands shown as colored background strips
- HR ZONE BANDS visible behind the graph:
  - Z1 gray, Z2 blue, Z3 green, Z4 yellow, Z5 orange, Z6 red (from LTHR zones)
  - Current HR zone highlighted with label: "Z4 Threshold"

HR ZONE INDICATOR (alongside other data fields):
- Large HR number (36pt) with zone color background
- Zone name label: "Z2 Endurance" / "Z4 Threshold" / "Z5 VO2max"
- Time-in-zone bar updating live (how long in current zone)
- HR zone boundaries from config.py: LTHR-based (Friel 5-zone model)

ERG / SIM MODE AUTO-SWITCHING:
- **Workout mode**: Auto-ERG. Sends SET_TARGET_POWER for each segment.
  During FreeRide segments within a workout → switch to SIM (flat 0% grade).
  When next structured segment starts → switch back to ERG.
- **Course mode**: Always SIM. Sends gradient from CRS data.
- **Hybrid mode**: SIM for gradient, but workout blocks override with ERG targets.
  When interval block active → ERG. Between blocks → SIM (route gradient).
- **Mode switching**: No FTMS handshake needed — just send the different command type.
  The trainer switches automatically based on which command it receives last.
- **Transition smoothing**: 3-second ramp between modes to prevent jarring resistance changes.
- **UI indicator**: "ERG 238W" or "SIM 4.2%" badge in top-right, changes color on mode switch.

IN-RIDE CONTROLS:
- [+5%] [−5%]: adjust trainer difficulty mid-ride (gradient scaling)
- [PAUSE]: pause timer + set 0% grade. [STOP]: end → summary screen
- Keyboard: Space=pause, +/-=difficulty, Esc=stop
- Road feel selector: dropdown to switch surface mid-ride

ROUTE MAP (if GPX available):
- Leaflet.js with gradient-colored polyline + moving position dot
- Auto-pan to keep rider centered
- Shows upcoming gradient preview (next 500m)
- Summit countdown distance

ROAD FEEL (TACX Neo feature):
- Surface types: Asphalt, Cobblestone (hard/soft), Gravel, Brick, Off-road, Ice, Wood
- Can auto-switch based on GPX route surface tags (if available)
- Manual override dropdown during ride
- Intensity slider (0-100%)
- Route library can tag surface types per course segment

END-OF-RIDE SUMMARY:
┌──────────────────────────────────────────────────────────────┐
│ RIDE COMPLETE — Stelvio Pass                                 │
│ ⏱ 1:23:45 │ 📏 23.4km │ ⛰️ 1596m │ ⚡avg 210W │ ❤️avg 158  │
│ NP 228W │ IF 0.91 │ TSS 142 │ W/kg 3.2 │ 🔄 82rpm │ 1240kcal│
│                                                              │
│ [Power chart SVG with zone coloring]                         │
│ [Zone distribution bar: Z1 12% Z2 45% Z3 18% Z4 15% ...]   │
│                                                              │
│ [Save] [Export FIT] [Upload to Intervals.icu] [Close]        │
└──────────────────────────────────────────────────────────────┘
```

**Power zone colors** (consistent with existing app):
- Z1 Recovery: `#4a90d9` (blue)
- Z2 Endurance: `#22c55e` (green)  
- Z3 Tempo: `#eab308` (yellow)
- Z4 Sweet Spot: `#f97316` (orange)
- Z5 Threshold: `#ef4444` (red)
- Z6 VO2max+: `#8b0000` (dark red)

**Deviation bar**: horizontal bar centered on target — green when ±5%, yellow ±10%, red >10%. Shows "+7W" or "-12W" text.

**W'bal gauge**: vertical bar on right side. Formulas:
- CP = 0.76 × FTP (Allen & Coggan)
- W' = FTP × 0.24 × 60 kJ (default)
- Depletion: `W'bal -= (P - CP) × dt` when P > CP
- Recovery: `W'bal += (W' - W'bal) × (1 - e^(-dt/tau))` when P < CP, tau = 546s (Skiba 2015)
- Flash red + warning text when W'bal < 20%

**Other derived metrics (training_live.py):**
- NP: 30s rolling avg of P⁴, then ⁴√ (Coggan)
- IF: NP / FTP
- TSS: (seconds × NP × IF) / (FTP × 3600) × 100
- XPower: 25s exponentially weighted power (Skiba)

**Mock data** (JS): `setInterval` at 1Hz generates realistic power oscillating around target ± noise, cadence 85-95, HR drift. Same WebSocket path as real BLE data.

---

## Phase 3: BLE Trainer Connection

**Goal**: Connect TACX (or any FTMS trainer) via Bluetooth. Read telemetry, send resistance.

### New files:
- **`trainer_connection.py`** — Unified trainer abstraction supporting 3 protocols:

  **Protocol 1: BLE FTMS (standard)** — Wahoo KICKR, Elite Direto, Saris H3, TACX Flux (any FTMS-compliant trainer)
  - Service UUID `0x1826`, Indoor Bike Data `0x2AD2`, Control Point `0x2AD9`
  - `set_target_inclination(grade)` → 0x03 command
  - `set_target_power(watts)` → 0x05 command (ERG)
  - `set_simulation_params(grade, wind, crr, cda)` → 0x11 command (full physics)
  - Uses `bleak` directly (FTMS is simple enough, no library needed)

  **Protocol 2: TACX Proprietary BLE** — TACX Neo 2T, Neo 1, Neo 2
  - Service UUID `6E40FEC1-B5A3-F393-E0A9-E50E24DCCA9E`
  - Uses `pycycling.TacxTrainerControl` for FE-C over BLE + road feel
  - `set_road_feel(surface, intensity)` → TACX-exclusive feature
    - Surfaces: CONCRETE_PLATES, CATTLE_GRID, COBBLESTONES_HARD/SOFT, BRICK_ROAD, OFF_ROAD, GRAVEL, ICE, WOODEN_BOARDS
  - Falls through to FTMS if TACX service not found (Neo also advertises FTMS)

  **Protocol 3: ANT+ FE-C** — ANY ANT+ trainer (older TACX, Wahoo, CycleOps, etc.)
  - Uses `openant` library with USB ANT+ dongle
  - Channel type 0x00, network key, device type 17 (FE-C)
  - `set_track_resistance(grade)` → page 51 (0x33)
  - `set_target_power(watts)` → page 49 (0x31)
  - Receives: page 25 (general data), page 26 (specific trainer data with power/cadence)
  - Fallback for trainers without BLE or when BLE is unreliable

  **Auto-detection flow** (simulator-style):
  ```
  1. Scan BLE for FTMS (0x1826) + TACX (6E40FEC1) + HR (0x180D)
  2. Scan ANT+ for FE-C devices (device type 17) + HR (device type 120)
  3. Present ALL found devices with protocol badge [BLE] or [ANT+]
  4. User picks trainer + HR source (can mix: BLE trainer + ANT+ HR or vice versa)
  5. Remember selection in known_devices.json → auto-connect next time
  ```

  **Trainer abstraction** — `BaseTrainer` ABC with:
  - `connect()`, `disconnect()`, `set_slope(grade, trainer_effect)`, `set_power(watts)`
  - `on_data(callback)` → receives `TrainerData(power, cadence, speed, hr, timestamp)`
  - Subclasses: `FTMSTrainer`, `TacxBLETrainer`, `ANTPlusFECTrainer`
  - All produce identical `TrainerData` output regardless of protocol

  **HR strap** — separate `HRMonitor` class:
  - BLE: standard 0x180D service, 0x2A37 characteristic
  - ANT+: device type 120, HR data page
  - Priority: external HR > trainer-relayed HR

  **Connection reliability** (from grill):
  - Reconnect: exponential backoff 1s→2s→4s→8s→16s (5 attempts)
  - Device lost: 15s of no data → pause ride, keep data
  - Gradient update: every 2 seconds, linear interpolation between CRS points, 2%/s max ramp rate
  - Startup: discard first 5s of data (BLE handshake garbage)
  - RSSI monitoring: warn user below -80 dBm

  **Device memory** (reference pattern): `known_devices.json`:
  ```json
  {
    "trainer": {"address": "12DF5F64-...", "name": "Tacx Neo 2T 19020", "protocol": "tacx_ble"},
    "hr": {"address": "499EBE6C-...", "name": "HRMPro+:581469", "protocol": "ble_hr"},
    "trainer_effect": 0.5,
    "road_feel_enabled": true
  }
  ```

### New routes in app.py:
- `GET /api/ble/scan` — trigger BLE scan, return devices
- `POST /api/ble/connect` — connect by address
- `GET /api/ble/status` — connection state

### Modified:
- `training.html` — "Connect Trainer" button with scan results + status LED
- `training_live.py` — wire BLE callbacks into TrainingSession (replaces mock data)
- `requirements.txt` — add `pycycling>=0.4.0`, `bleak>=0.22.0`, `openant>=1.0.0`

**Data flow:**
```
TACX (BLE FTMS 1Hz) → pycycling → TrainingSession.update()
  → compute NP/TSS/W'bal/XPower → WebSocket → training.html
```

**BLE + asyncio**: bleak is async-native, FastAPI runs asyncio — they integrate cleanly. BLE scan runs as background asyncio task (~5s).

---

## Phase 4: Full Ride Execution (CRS + ZWO)

**Goal**: CRS files drive trainer slope in real-time. ZWO workouts drive ERG power targets. Everything feeds the live display.

### Execution modes in training_live.py:

**Course mode** (CRS slope simulation):
```
speed (from trainer) × dt → advance routeDistance
→ binary search CRS points for current grade
→ ble_trainer.set_slope(grade)  [or set_simulation() for full physics]
→ update elevation marker on profile SVG
```

**Workout mode** (ERG power targets):
```
elapsed_time → find current ZWO segment
→ ble_trainer.set_target_power(segment.watts)
→ handle IntervalsT repeats, ramps (interpolate PowerLow → PowerHigh)
→ advance workout block "now" marker
```

**Hybrid** (course + workout overlay): Grade drives resistance, workout blocks overlay timing structure.

### Session launcher in training.html:
- Mode picker: Free Ride / Course / Workout / Hybrid
- Course: reuse `/api/courses` list (existing)
- Workout: reuse `/api/workouts` list (existing)
- "Start Ride" → `POST /api/training/start` with `{mode, course_file, workout_file}`

### End-of-ride summary:
- Duration, distance, avg/max/NP power, IF, TSS, elevation gain
- Export as FIT file (reuse existing `_build_fit_workout()` pattern)
- Save to database → appears in fitness chart + training load

### Modified:
- `db.py` — `training_sessions` table for completed indoor rides
- `app.py` — `/api/training/summary`, `/api/training/save`

---

## Implementation Order

| # | Phase | New Files | Key Dependencies |
|---|-------|-----------|------------------|
| 1 | Native window | — | pywebview |
| 2 | Training UI + mock | training.html, training_live.py | websockets |
| 3 | BLE trainer | ble_trainer.py | pycycling, bleak |
| 4 | Full ride execution | — | phases 2+3 |

## Route Surface Types (road feel library)

**Extend the course library with surface type metadata.** Each CRS/GPX route can have surface segments:

```json
{
  "surface_segments": [
    {"start_km": 0.0, "end_km": 2.0, "surface": "asphalt"},
    {"start_km": 2.0, "end_km": 5.5, "surface": "cobblestone_hard"},
    {"start_km": 5.5, "end_km": 8.0, "surface": "gravel"},
    {"start_km": 8.0, "end_km": 11.0, "surface": "asphalt"}
  ]
}
```

**New route types in library:**
- **Road**: Asphalt (default)
- **Cobbled classics**: Paris-Roubaix, Tour of Flanders — cobblestone_hard/soft
- **Gravel**: Strade Bianche, L'Eroica, gravel events — gravel/off_road
- **Mixed**: Real-world routes with varying surfaces

**Source**: Surface types from GPX `<extensions>` tags (some GPX files include surface info), or manually tagged per course.

**During ride**: `training_live.py` looks up current `routeDistance` against surface_segments → sends `set_road_feel(surface)` to TACX Neo → rider feels the road change.

---

## Critical Files

| File | Role |
|------|------|
| `launcher.py` | Native window (pywebview) |
| `templates/training.html` | Full-screen ride display |
| `training_live.py` | Session state + derived metrics (NP/TSS/W'bal) |
| `ble_trainer.py` | FTMS trainer communication (pycycling) |
| `app.py` | WebSocket endpoint + BLE/training routes |
| `db.py` | Persist completed rides |

## Verification

1. **Phase 1**: App opens in native window with chicken icon. Not a browser tab.
2. **Phase 2**: `/training` shows full-screen dark display. Mock data updates 1Hz. Power 56pt readable at arm's length. W'bal gauge changes color. Elevation marker moves. Workout blocks advance.
3. **Phase 3**: "Connect Trainer" finds TACX. Real power/HR/cadence replaces mock. Deviation bar shows +/-W from target.
4. **Phase 4**: Start course → trainer resistance changes with CRS gradient. Start workout → ERG targets per segment. End-of-ride → correct TSS/NP/W'bal summary. Saved ride appears in fitness chart.
