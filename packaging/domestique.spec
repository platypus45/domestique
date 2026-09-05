# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Domestique.

Build:
  macOS:   pyinstaller domestique.spec
  Windows: pyinstaller domestique.spec
  Linux:   ./build_linux.sh   (wraps the onedir output in an AppImage)

Output: dist/Domestique.app (macOS) or dist/Domestique/Domestique[.exe]
"""

import sys
import os
from pathlib import Path

block_cipher = None
app_name = "Domestique"

# Every Linux arm below is guarded on this so a darwin/win32 PyInstaller run
# produces byte-identical output to before the Linux release existed.
_LINUX = sys.platform.startswith("linux")

# Single source of truth for the bundle version — read the repo's VERSION
# file at build time. `SPEC` is the absolute path to this spec file that
# PyInstaller injects into the spec namespace. Falling back to CWD keeps
# this robust if a future PyInstaller version drops `SPEC`.
try:
    _spec_dir = Path(SPEC).resolve().parent  # type: ignore[name-defined]
except NameError:
    _spec_dir = Path(os.path.abspath(os.getcwd()))
VERSION = (_spec_dir.parent / "VERSION").read_text(encoding="utf-8").strip()

# Data files to bundle (templates, courses, JSON, CSV).
# All entries unconditional — these directories MUST exist for the app to run
# and conditional guards caused silent "missing assets" bugs in older builds.
datas = [
    ("../src/templates", "templates"),
    ("../src/courses", "courses"),
    ("../src/workouts", "workouts"),          # 1,753 scientific workout templates
    ("../src/static", "static"),
    ("../assets", "assets"),
    ("../src/routes.json", "."),
    ("../src/profiles_indexed.json", "."),
    ("../src/surface_types.json", "."),
    ("../VERSION", "."),
    # 3.3.1 hotfix (v3.3.0 storm): workout_facts._clc() loads the classifier
    # by FILE PATH (Path(__file__).parent / "scripts" / "classify_library_
    # content.py"), so PyInstaller's import scan never sees it. v3.3.0
    # shipped without it → every frozen facts recompute raised
    # FileNotFoundError → all-null facts → planner-wide no-candidates.
    # Stdlib-only at module level, so bundling the single file suffices.
    ("../src/scripts/classify_library_content.py", "scripts"),
]

# v2.1.0 WIN-TLS-FIX: bundle certifi's CA bundle so urllib (all ICU HTTPS in
# training.py/db.py) can verify certs in the frozen Windows build. Without it
# urllib has no CA store → cert-verify fails → "ICUNetworkError" on every
# credential save / sync. httpx ships certifi; urllib needs the file on disk +
# SSL_CERT_FILE (set in launcher.configure_tls_ca).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
datas += collect_data_files("certifi")

binaries: list = []

if _LINUX:
    # QtWebEngine is a multi-process engine: the window is drawn by a SEPARATE
    # helper binary, plus its .pak resources and locale files. PyInstaller's
    # PySide6 hook is supposed to collect these, and on one build host it did
    # while on another it silently did not — leaving a bundle with every
    # libQt6WebEngine*.so present and no QtWebEngineProcess to run them. The
    # window then opens and renders nothing, which is the one failure this
    # whole release exists to avoid. Collect them explicitly instead of
    # depending on hook behaviour.
    import PySide6 as _ps6
    _qt = Path(_ps6.__file__).parent / "Qt"
    _helper = _qt / "libexec" / "QtWebEngineProcess"
    if not _helper.exists():
        raise SystemExit(
            f"FATAL: {_helper} missing from the PySide6 wheel — a Linux build "
            "without it produces a window that draws nothing.")
    binaries += [(str(_helper), "PySide6/Qt/libexec")]
    for _sub in ("resources", "translations/qtwebengine_locales"):
        _d = _qt / _sub
        if _d.is_dir():
            for _f in _d.rglob("*"):
                if _f.is_file():
                    datas.append((str(_f), str(Path("PySide6/Qt") / _sub)))

# Add profiles directory if it exists (per-user data, optional)
if os.path.exists("src/profiles"):
    datas.append(("../src/profiles", "profiles"))

# Add gpx directory if it exists (optional user imports)
if os.path.exists("src/gpx"):
    datas.append(("../src/gpx", "gpx"))

# v2.1.x ICU OAuth — bundle the gitignored .oauth.env (client_secret) so the
# frozen app carries it while the PUBLIC repo never does (see config._load_oauth_env).
# The secret necessarily ships in the binary (installed-app OAuth, no PKCE).
if os.path.exists(".oauth.env"):
    datas.append(("../.oauth.env", "."))

# Previously we enumerated top-level `.py` modules explicitly and added them
# as `datas`. PyInstaller's Analysis pass already picks them up via its
# static-import scan from `launcher.py` → `app.py` → the rest of the tree,
# so the explicit list was redundant (and rotted whenever a module was
# added/removed). Dropping it keeps the spec short and correct.

a = Analysis(
    ["../src/launcher.py"],
    pathex=["..", "../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "certifi",  # v2.1.0 WIN-TLS-FIX — urllib CA bundle (see datas + launcher)
        # v3.11.4 WIN-TLS-TRUST — OS-native verifier for intervals.icu on Windows.
        # The backends sit behind platform checks; name them so no analysis
        # shortcut can drop one (CI asserts tls_backend == os-native on Windows).
        "truststore", "truststore._api", "truststore._windows",
        "truststore._macos", "truststore._openssl", "truststore._ssl_constants",
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
        # v3.8.0 LINUX-BACKEND: same bug class as the win32 block above —
        # pywebview resolves its GUI backend lazily inside `webview.start()`,
        # so the static scan never reaches the Qt platform module and the
        # frozen app would raise WebViewException with no window. The Linux
        # release deliberately uses Qt/PySide6 (QtWebEngine ships its own
        # Chromium, so there is no host WebKitGTK to depend on). QtWebChannel
        # is NOT optional: webview/platforms/qt.py routes the js_api bridge
        # through QWebChannel, and its absence sends qt.py down the PyQt5
        # QtWebKit fallback branch, which does not exist here. Linux-only, as
        # PySide6 is not installed on the macOS/Windows builders.
        *(["qtpy", "webview.platforms.qt", "PySide6",
           "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
           "PySide6.QtNetwork", "PySide6.QtWebChannel",
           "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"]
          if _LINUX else []),
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
        # v3.11.2: fit_tool's message_factory imports EVERY message module
        # (90 of them) — but only reachable from a lazy import inside
        # try/except, so the static scan never followed it and every frozen
        # build lacked device_info_message & co. A real Garmin FIT always
        # carries device_info → "No module named ..." → the whole ride parse
        # failed (Linux report, every platform). Collect them all.
        *collect_submodules("fit_tool.profile.messages"),
        "fit_tool.profile.messages.message_factory",
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
    # UPX mangles the QtWebEngine shared libraries badly enough that the
    # process dies at load; harmless-to-helpful on the other two platforms.
    upx=not _LINUX,
    console=False,  # no terminal window
    # An ELF carries no embedded icon, and handing PyInstaller the .ico here
    # would only make it try (and fail) to stamp a Windows resource onto one.
    # The Linux icon travels in the AppDir's .desktop + hicolor tree instead.
    icon=(None if _LINUX else
          "../assets/icon.icns" if sys.platform == "darwin" else "../assets/icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=not _LINUX,  # see EXE — UPX corrupts the bundled Qt libraries
    upx_exclude=[],
    name=app_name,
)

# macOS: create .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{app_name}.app",
        icon="../assets/icon.icns" if os.path.exists("assets/icon.icns") else None,
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
