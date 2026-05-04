# Implementation Plan: App Packaging + Setup Wizard

## Architecture Decision: PyInstaller + pystray

**Why PyInstaller**: The app opens in the default browser (no bundled webview needed), eliminating Electron/Tauri's main value. PyInstaller gives the smallest bundle (~50-80MB), simplest toolchain (no Rust/Node), and builds on both macOS and Windows with the same tool.

**Runtime flow**:
```
User double-clicks app → launcher.py starts →
  1. Start FastAPI server on localhost:8080
  2. Open default browser to http://localhost:8080
  3. Show system tray icon (quit/restart/open browser)
  4. If first launch → browser opens to /setup wizard
```

---

## Phase 1: Setup Wizard (browser-based, in existing app)

A multi-step first-launch wizard at `/setup` that configures everything before showing the main dashboard.

### Step 1: Welcome + Intervals.icu Connection
- Input fields: `Athlete ID` (e.g. i225278) and `API Key`
- "How to find these" link with screenshot/instructions
- "Test Connection" button → hits `/api/setup/test-icu` → tries fetching wellness data
- Green checkmark on success, error message on failure
- Saves to `.env` file

### Step 2: Athlete Profile
- Weight (kg), FTP (watts), LTHR (bpm), Max HR (bpm)
- If Intervals.icu connected: auto-fill eFTP + weight from API
- LBM auto-calculated (weight × 0.80 default, editable)

### Step 3: iCloud Food Log (optional)
- Explain the food_log.txt format
- Auto-detect if `~/Library/Mobile Documents/com~apple~CloudDocs/` exists (macOS)
- Browse/set custom path on Windows
- Skip button (food tracking is optional)

### Step 4: Training Preferences
- Hours per week available (slider: 4-15h)
- Available training days (checkboxes Mon-Sun)
- Rest days (checkboxes)
- Goal type: FTP / VO2max / Hybrid / Event prep

### Step 5: Done
- Summary of configuration
- "Open Dashboard" button → redirect to `/`
- Setup state saved to `.setup_complete` marker file

### Backend endpoints (new):
```
GET  /setup              → setup wizard HTML page
POST /api/setup/test-icu → test Intervals.icu credentials
POST /api/setup/save     → save all wizard settings (.env + config.py)
GET  /api/setup/status   → check if setup is complete
```

### Auto-redirect logic:
In `app.py` dashboard route: if `.setup_complete` doesn't exist, redirect to `/setup`.

---

## Phase 2: Launcher + System Tray (new file: launcher.py)

```python
# launcher.py — entry point for packaged app
import subprocess, webbrowser, sys, time
from pystray import Icon, MenuItem, Menu
from PIL import Image

def start_server():
    # Start FastAPI in subprocess
    proc = subprocess.Popen([sys.executable, "app.py"])
    time.sleep(2)  # wait for server to start
    webbrowser.open("http://localhost:8080")
    return proc

def create_tray(proc):
    # System tray icon with quit/open options
    icon = Icon("Health Tracker", image, menu=Menu(
        MenuItem("Open Dashboard", lambda: webbrowser.open("http://localhost:8080")),
        MenuItem("Restart Server", restart_server),
        MenuItem("Quit", lambda: (proc.terminate(), icon.stop())),
    ))
    icon.run()
```

---

## Phase 3: PyInstaller Packaging

### New files:
- `launcher.py` — entry point (tray icon + server management)
- `health_tracker.spec` — PyInstaller spec file
- `build_mac.sh` — build script for macOS .app + .dmg
- `build_win.bat` — build script for Windows .exe
- `assets/icon.icns` + `assets/icon.ico` — app icons
- `requirements.txt` — pinned dependencies

### PyInstaller spec highlights:
```python
# health_tracker.spec
a = Analysis(['launcher.py'],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('courses', 'courses'),
        ('zwift_routes.json', '.'),
        ('workout_analysis.csv', '.'),
        ('*.py', '.'),  # config.py, training.py, etc.
    ],
    hiddenimports=['uvicorn.logging', 'uvicorn.protocols.http'],
)
```

### Build outputs:
- **macOS**: `dist/Health Tracker.app` → wrap with `create-dmg` → `Health_Tracker.dmg`
- **Windows**: `dist/Health Tracker.exe` → wrap with Inno Setup → `Health_Tracker_Setup.exe`

---

## File Changes Summary

| File | Action | Description |
|------|--------|-------------|
| `templates/setup.html` | CREATE | Setup wizard UI (5-step form) |
| `app.py` | EDIT | Add `/setup` route, `/api/setup/*` endpoints, auto-redirect |
| `launcher.py` | CREATE | Entry point with tray icon + server management |
| `health_tracker.spec` | CREATE | PyInstaller build configuration |
| `requirements.txt` | CREATE | Pinned dependencies for reproducible builds |
| `build_mac.sh` | CREATE | macOS build script (.app → .dmg) |
| `build_win.bat` | CREATE | Windows build script (.exe → installer) |
| `assets/icon.icns` | CREATE | macOS app icon |
| `assets/icon.ico` | CREATE | Windows app icon |

---

## Implementation Order

1. **Setup wizard** (templates/setup.html + API endpoints) — usable immediately without packaging
2. **Launcher** (launcher.py + pystray) — test locally
3. **requirements.txt** — pin all dependencies
4. **PyInstaller spec** — build and test on macOS
5. **Build scripts** — automate .dmg/.exe creation
6. **Windows build** — test on Windows (or CI with GitHub Actions)
