# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Domestique.

Build:
  macOS:   pyinstaller domestique.spec
  Windows: pyinstaller domestique.spec

Output: dist/Domestique.app (macOS) or dist/Domestique/Domestique.exe (Windows)
"""

import sys
import os
from pathlib import Path

block_cipher = None
app_name = "Domestique"

# Single source of truth for the bundle version — read the repo's VERSION
# file at build time. `SPEC` is the absolute path to this spec file that
# PyInstaller injects into the spec namespace. Falling back to CWD keeps
# this robust if a future PyInstaller version drops `SPEC`.
try:
    _spec_dir = Path(SPEC).resolve().parent  # type: ignore[name-defined]
except NameError:
    _spec_dir = Path(os.path.abspath(os.getcwd()))
VERSION = (_spec_dir / "VERSION").read_text(encoding="utf-8").strip()

# Data files to bundle (templates, courses, JSON, CSV).
# All entries unconditional — these directories MUST exist for the app to run
# and conditional guards caused silent "missing assets" bugs in older builds.
datas = [
    ("templates", "templates"),
    ("courses", "courses"),
    ("workouts", "workouts"),          # 1,753 scientific workout templates
    ("static", "static"),
    ("assets", "assets"),
    ("routes.json", "."),
    ("profiles_indexed.json", "."),
    ("surface_types.json", "."),
    ("VERSION", "."),
]

# Add profiles directory if it exists (per-user data, optional)
if os.path.exists("profiles"):
    datas.append(("profiles", "profiles"))

# Add gpx directory if it exists (optional user imports)
if os.path.exists("gpx"):
    datas.append(("gpx", "gpx"))

# Previously we enumerated top-level `.py` modules explicitly and added them
# as `datas`. PyInstaller's Analysis pass already picks them up via its
# static-import scan from `launcher.py` → `app.py` → the rest of the tree,
# so the explicit list was redundant (and rotted whenever a module was
# added/removed). Dropping it keeps the spec short and correct.

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # v4.0.0-alpha: BLE/ANT+ runtime is gone with the trainer rip;
        # WebSocket bits are no longer imported because the /ws/training
        # endpoint was deleted. Keep only the HTTP + lifespan minimum.
        "uvicorn.logging",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "fastapi",
        "starlette.routing",
        "starlette.responses",
        "starlette.staticfiles",
        "starlette.templating",
        "jinja2",
        "httpx",
        "pydantic",
        "pystray",
        "PIL",
        "webview",
        # v2.0.2 WIN-START-FIX: pywebview's Windows backend (EdgeChromium /
        # WinForms) loads the .NET CLR through pythonnet at runtime. None of
        # these are reachable by PyInstaller's static import scan from
        # `webview` (they're imported lazily by `webview.start()` after a
        # platform probe), so on a frozen windowed Windows EXE the backend
        # failed to import and `webview.start()` died silently. Force them in
        # — but ONLY on a Windows build, since `clr` / pythonnet do not exist
        # on macOS and listing them there would break the (working) DMG
        # build. A darwin PyInstaller run evaluates the guard to `[]`.
        *(["clr", "clr_loader", "webview.platforms.edgechromium",
           "webview.platforms.winforms", "proxy_tools"]
          if sys.platform == "win32" else []),
        # v1.0.1: fit_tool is imported lazily inside try/except in app.py (FIT
        # workout export endpoint + .fit ride parser). PyInstaller's static
        # analyser misses imports inside try/except blocks, so the module was
        # absent from the bundled DMG/EXE — every "Download FIT" click hit the
        # `except ImportError` branch and returned a 500 JSON which the dashboard
        # silently swallowed. Listing the relevant submodules here forces them
        # into the frozen archive.
        "fit_tool",
        "fit_tool.fit_file",
        "fit_tool.fit_file_builder",
        "fit_tool.profile.profile_type",
        "fit_tool.profile.messages.file_id_message",
        "fit_tool.profile.messages.workout_message",
        "fit_tool.profile.messages.workout_step_message",
        # v1.0.7 IMPL-TAU-FIT-CORE: scipy is now a hard dependency for
        # tau_fitting.py (Banister NLS via scipy.optimize.curve_fit +
        # bootstrap-CI). PyInstaller's static analyser misses scipy's lazy
        # `_lib.array_api_compat` shim, which scipy.optimize imports on
        # first call — without these explicit hidden imports the frozen
        # bundle raises ModuleNotFoundError on the first τ-fit run.
        "scipy",
        "scipy.optimize",
        "scipy.linalg",
        "scipy._lib.array_api_compat",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # v1.0.7: numpy + scipy were previously excluded for a leaner DMG; both are
    # now required for τ-fitting and must NOT be excluded. matplotlib + pandas
    # remain excluded — Domestique never imports them. (~50 MB bundle hit, see
    # MASTER_DECISIONS_v107_v110_v120_PATCH.md G2.)
    excludes=["matplotlib", "pandas"],  # keep tkinter for folder picker
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # no terminal window
    icon="assets/icon.icns" if sys.platform == "darwin" else "assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=app_name,
)

# macOS: create .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{app_name}.app",
        icon="assets/icon.icns" if os.path.exists("assets/icon.icns") else None,
        bundle_identifier="com.platypus45.domestique",
        info_plist={
            "CFBundleDisplayName": app_name,
            # Both keys MUST match VERSION — otherwise the About box and the
            # release-artifact build number drift.
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            # Default to Apple-Silicon-era (macOS 11) since the CI builders
            # produce arm64 + x86_64 universal binaries and Apple Silicon
            # itself requires macOS 11. Drop back to "10.15" only if Intel
            # Mojave/Catalina support becomes a hard requirement.
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "(c) 2026 Domestique",
            # v4.0.0-alpha: Bluetooth usage key removed along with the BLE
            # subsystem -- Domestique no longer scans or connects to any
            # trainer/HR device. Keeping the key would spuriously trigger
            # the Apple TCC prompt on first launch.
            "LSUIElement": False,  # show in Dock
        },
    )
