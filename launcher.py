#!/usr/bin/env python3
"""
Domestique Launcher — entry point for the packaged desktop app.

Starts the FastAPI server IN-PROCESS (not as subprocess, to avoid PyInstaller
fork issues), opens the browser, and shows a system tray icon.

Works both in dev mode (python launcher.py) and frozen mode (PyInstaller).
"""

import base64
import multiprocessing
import os
import sys
import threading
import time
import webbrowser
import signal

# Prevent PyInstaller frozen multiprocessing fork bomb
multiprocessing.freeze_support()

# WIN-ENCODING-FIX: make stdout/stderr bulletproof BEFORE any print(). Two
# Windows-only failure modes this prevents — each crashes the launcher with an
# unhandled exception → silent exit(1) → "the app doesn't start at all":
#   1. Frozen *windowed* build (console=False): sys.stdout/err are None, so
#      ANY print() raises AttributeError on None.write.
#   2. Frozen *console* build: stdout is cp1252, so a non-ASCII glyph in a
#      status line (we use → and — liberally) raises UnicodeEncodeError —
#      this is the exact crash a Windows user hit at "Server ready → …".
# Normalize both: None → a discarding stream; real streams → UTF-8 with
# errors="replace" so an un-encodable glyph degrades to '?' instead of killing
# the process. macOS/Linux already default to UTF-8, so this is a no-op there.
def _harden_std_streams():
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is None:
            try:
                setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_harden_std_streams()


# WIN-TLS-FIX (v2.0.8): in a frozen Windows build urllib has no usable CA store,
# so HTTPS to intervals.icu fails cert verification → URLError → "ICUNetworkError"
# on every credential save / sync. (httpx works because it bundles certifi; urllib
# uses the OS default SSL context, which is empty in a frozen Windows app.) Point
# urllib's default context at certifi's bundled CA file via SSL_CERT_FILE. Win32-only
# so the working macOS build (system certs) stays byte-identical. Must run before
# any HTTPS call (i.e. before start_server) — top-level here guarantees that.
def configure_tls_ca(platform=None):
    """Set SSL_CERT_FILE/SSL_CERT_DIR to certifi's CA bundle on Windows so urllib
    can verify HTTPS. Returns the CA path set, or None when not applicable.
    Uses setdefault so a user-provided SSL_CERT_FILE is respected."""
    plat = platform if platform is not None else sys.platform
    if plat != "win32":
        return None
    try:
        import certifi
        ca = certifi.where()
    except Exception:
        return None
    os.environ.setdefault("SSL_CERT_FILE", ca)
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(ca))
    return ca


configure_tls_ca()

# Port 8080 is PINNED for single-instance hygiene: the tray icon + existing
# instance guard both assume localhost:8080. If we let uvicorn float the
# port, the single-instance detection breaks and desktop shortcuts that
# point at :8080 stop working across restarts.
# (Master decisions §3 — fail loudly if 8080 is busy.)
PORT = 8080
URL = f"http://localhost:{PORT}"


def _log():
    """Best-effort launcher logger that writes to ~/.domestique/logs/.

    v2.0.2 WIN-START-FIX: a frozen *windowed* build (console=False) has a
    dead stdout, so every startup `print()` here vanishes. Mirroring the
    diagnostics through log_config leaves a trace on disk
    (~/.domestique/logs/domestique_<ts>.log) so a "nothing happened"
    Windows launch is actually diagnosable. Returns None if log_config
    can't be imported (e.g. partial bundle) — callers must tolerate that.
    """
    try:
        import log_config
        return log_config.get_logger("domestique.app")
    except Exception:
        return None


def _is_server_only() -> bool:
    """True when the launcher should run headless (server, no window/tray).

    v2.0.2 WIN-CI-SMOKE: CI needs to confirm a frozen Windows build actually
    boots and serves the right version, but a headless GitHub runner has no
    display — calling webview.start() or run_with_tray() would block forever
    waiting on a GUI/tray loop that can never appear. When DOMESTIQUE_SERVER_ONLY=1
    (or --server-only is passed) we start the server via the normal path, wait
    for it to come up, then keep-alive without ever touching pywebview/pystray.
    The flag is opt-in: when it is unset every existing path is unchanged.
    """
    return os.environ.get("DOMESTIQUE_SERVER_ONLY") == "1" or "--server-only" in sys.argv


def _ensure_port_free_or_die() -> None:
    """Refuse to start if another process is already bound to port 8080.

    The single-instance branch in `main()` handles the "Domestique already
    running" case before this gets called — so any other listener on 8080
    here is some unrelated app squatting the port. Exit with a clear
    message rather than silently picking another port (which would break
    single-instance detection and any saved :8080 shortcuts).

    NOTE — we deliberately do NOT set SO_REUSEADDR on this probe. With
    REUSEADDR a Linux TIME_WAIT socket from a prior crashed instance lets
    the bind succeed; uvicorn (which doesn't set REUSEADDR by default) then
    fails immediately afterwards with a less-clear error than the FATAL
    message advertised here. Probing without REUSEADDR matches uvicorn's
    own bind semantics so a successful probe predicts a successful uvicorn
    start. There is still a tiny TOCTOU window between this probe and
    uvicorn's bind, but if some other process grabs 8080 in that window
    uvicorn's "address already in use" error is itself a clear signal.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError as e:
        msg = (
            f"FATAL: cannot bind 127.0.0.1:{PORT} ({e}). "
            f"Domestique requires port {PORT} for single-instance detection. "
            f"Stop the conflicting process and try again."
        )
        print(f"\n{msg}\n")
        # v2.0.2 WIN-START-FIX: a windowed build's stdout is dead, so this
        # sys.exit(2) would otherwise be a silent death. Leave a trace.
        log = _log()
        if log is not None:
            log.error(msg)
        sys.exit(2)
    finally:
        try:
            s.close()
        except OSError:
            pass


_server_thread = None
_shutdown_event = threading.Event()
# Holds the last exception raised inside the uvicorn thread, if any.
# The main thread polls this after wait_for_server() fails so the user
# sees the real traceback instead of a generic "server didn't start".
_server_error: "Exception | None" = None
# Holds the FULL formatted traceback (traceback.format_exc()) from the
# uvicorn thread. str(_server_error) loses the stack; in a frozen windowed
# build the uvicorn/app-startup traceback is the single most useful artifact
# for diagnosing a silent "connection refused" startup death, so capture it
# verbatim for the on-disk log and CI stdout.
_server_traceback: "str | None" = None
# CON5: handle to the running uvicorn Server so SIGTERM/SIGINT can flip
# `should_exit = True` and let the FastAPI lifespan run to completion
# instead of sys.exit() killing the process mid-shutdown.
_uvicorn_server = None


def get_app_dir():
    """Return the app directory — handles both dev and frozen (PyInstaller) mode."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def start_server():
    """Start the FastAPI server in a background thread."""
    app_dir = get_app_dir()

    # Ensure app_dir is on sys.path so imports work
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    # Set working directory to app_dir for file-relative paths
    os.chdir(app_dir)

    def _run():
        global _server_error, _server_traceback, _uvicorn_server
        try:
            import uvicorn
            # Import the app module — this triggers all the FastAPI setup
            from app import app
            # CON5: use Config/Server so the signal handler can request a
            # graceful shutdown (which runs the FastAPI lifespan teardown)
            # instead of sys.exit() hard-killing the process.
            config = uvicorn.Config(
                app, host="127.0.0.1", port=PORT, log_level="warning"
            )
            _uvicorn_server = uvicorn.Server(config)
            _uvicorn_server.run()
        except Exception as e:
            # Daemon threads swallow exceptions silently, producing a
            # confusing "server didn't start" with no traceback. Capture
            # the FULL traceback (not just str(e)) into _server_traceback
            # and log here so the main thread can surface the real cause —
            # in a frozen windowed build this on-disk traceback is the only
            # window into a silent uvicorn/app-startup failure.
            import traceback
            _server_error = e
            _server_traceback = traceback.format_exc()
            # WIN-DIAG: dead-simple crash dump that depends on NOTHING —
            # not log_config (may fail to init on Windows) and not stdout
            # capture (CI's Start-Process redirect came back empty). Plain
            # write_text to a fixed path so the cause is ALWAYS recoverable
            # by CI and by users on a silent windowed build.
            try:
                from pathlib import Path as _P
                _crash = _P.home() / ".domestique" / "startup_crash.txt"
                _crash.parent.mkdir(parents=True, exist_ok=True)
                _crash.write_text(_server_traceback, encoding="utf-8")
            except Exception:
                pass
            try:
                import log_config
                log_config.get_logger(__name__).exception(
                    "uvicorn server thread crashed"
                )
            except Exception:
                # Fall back to stderr if log_config fails during import
                traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_for_server(timeout=30):
    """Wait until the server is responding.

    Bumped default timeout to 30s — cold-start on slow disks (first frozen
    import of FastAPI + webview) can exceed 15s on low-end hardware.
    Bails out early if the uvicorn thread already raised.
    """
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        if _server_error is not None:
            return False
        try:
            urllib.request.urlopen(URL, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def run_with_tray():
    """Run system tray icon (requires pystray + Pillow)."""
    try:
        from pystray import Icon, MenuItem, Menu
        from PIL import Image, ImageDraw

        # Create icon: blue circle with white "H"
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(79, 108, 247))
        draw.text((22, 16), "H", fill="white")

        def open_browser(icon, item):
            webbrowser.open(URL)

        def quit_app(icon, item):
            _shutdown_event.set()
            icon.stop()

        menu = Menu(
            MenuItem("Open Dashboard", open_browser, default=True),
            MenuItem("Quit", quit_app),
        )

        icon = Icon("Domestique", img, "Domestique", menu)
        icon.run()

    except ImportError:
        # pystray not installed — block until Ctrl+C
        print("(pystray not installed — running without system tray)")
        print(f"Domestique → {URL}")
        print("Press Ctrl+C to quit.")
        try:
            _shutdown_event.wait()
        except KeyboardInterrupt:
            pass


def is_already_running():
    """Check if another instance is already serving on our port.

    Differentiates failure modes so operators can distinguish:
      - URLError: connection refused → port is free, not already running.
      - PermissionError (EACCES): firewall / app-sandbox blocks localhost.
    """
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(URL, timeout=1)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, PermissionError):
            print(
                f"Cannot probe {URL}: permission denied "
                f"({reason}). Check firewall / sandbox settings."
            )
            return False
        # ConnectionRefusedError or generic URLError — port is free
        return False
    except Exception:
        # Unknown error; treat as "not running" but note it.
        return False


def _activate_existing_window() -> bool:
    """Bring the existing Domestique native window to the foreground.

    Returns True on success, False if we should fall back to opening a browser.
    """
    if sys.platform == "darwin":
        # The pywebview native app registers as "Domestique" (see BUNDLE
        # in domestique.spec). Tell System Events to activate it. This
        # avoids the annoying browser-tab fallback when the user double-clicks
        # the .app while a previous instance is still running.
        try:
            import subprocess
            # Try bundle id first (set in domestique.spec Info.plist)
            res = subprocess.run(
                ["osascript", "-e",
                 'tell application id "com.platypus45.domestique" to activate'],
                capture_output=True, timeout=3,
            )
            if res.returncode == 0:
                return True
            # Fallback: activate by process name
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to '
                 'set frontmost of first process whose name is "Domestique" to true'],
                capture_output=True, timeout=3,
            )
            return True
        except Exception:
            return False
    elif sys.platform == "win32":
        # On Windows, pywebview creates a window with the app title. Use
        # user32.SetForegroundWindow via ctypes so we don't need a new dep.
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Domestique")
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return True
        except Exception:
            return False
    return False


class JsApi:
    """Native-save bridge exposed to dashboard JS as ``window.pywebview.api``.

    WKWebView (pywebview's macOS backend) silently ignores the HTML5
    ``<a download>`` attribute — clicking a "Download ZWO" link just
    navigates to the URL and renders the ZWO inline as text, and a
    synthetic anchor click in ``downloadFIT()`` does nothing at all. This
    bridge lets the JS hand a payload to Python and pop a native save
    dialog instead.

    Only ``save_zwo`` / ``save_fit`` are exposed — pywebview makes every
    public attribute of the api object callable from JS, so we
    deliberately don't add anything else here.
    """

    def _save(self, filename: str, data: bytes, file_types) -> dict:
        try:
            import webview

            # webview.windows is populated by webview.start() — at the
            # moment JS calls in, there's exactly one window (the main one).
            if not webview.windows:
                return {"ok": False, "error": "no window"}
            window = webview.windows[0]
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
                file_types=file_types,
            )
            if not result:
                return {"ok": False, "error": "cancelled"}
            # pywebview returns either a string (some platforms) or a
            # sequence of strings — normalise.
            path = result if isinstance(result, str) else result[0]
            with open(path, "wb") as f:
                f.write(data)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def save_zwo(self, filename: str, content: str) -> dict:
        return self._save(
            filename,
            content.encode("utf-8"),
            ("ZWO Workout (*.zwo)", "All files (*.*)"),
        )

    def save_fit(self, filename: str, content_b64: str) -> dict:
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception as e:
            return {"ok": False, "error": f"base64 decode failed: {e}"}
        return self._save(
            filename,
            data,
            ("FIT Workout (*.fit)", "All files (*.*)"),
        )


def _fallback_to_browser(reason: str) -> None:
    """Open the dashboard in the default browser when the native window fails.

    v2.0.2 WIN-START-FIX: the native pywebview window is the normal UI; this
    is the degraded path. On a frozen *windowed* Windows build the user sees
    no console, so silently opening a browser tab looked identical to "the
    app didn't launch". Three things happen here:
      1. The reason is mirrored to the on-disk log (windowed stdout is dead).
      2. The browser is opened.
      3. On Windows ONLY, a native MessageBox tells the user where the UI
         went, so the launch never *looks* like a no-op. The message box is
         best-effort (guarded by try/except) and is skipped on macOS/Linux,
         whose paths are deliberately left unchanged.
    """
    print(f"({reason} — opening in browser)")
    log = _log()
    if log is not None:
        log.error("native window unavailable (%s); opened browser at %s", reason, URL)
    webbrowser.open(URL)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Domestique's built-in window could not start "
                f"({reason}).\n\nIt has opened in your default web browser "
                f"at {URL} instead.",
                "Domestique",
                0x40,  # MB_ICONINFORMATION
            )
        except Exception:
            pass
    run_with_tray()


def main():
    # Single-instance guard: if another instance is already serving on our
    # port, activate its native window instead of opening a browser tab.
    # Opening Chrome/Safari defeats the whole point of the pywebview app.
    if is_already_running():
        if _activate_existing_window():
            print(f"Domestique already running — activated existing window.")
            return
        # Last resort: if we can't find the window (user killed the pywebview
        # process but something else is holding the port), open the browser so
        # the user can at least reach the UI.
        print(f"Domestique already running → {URL}")
        webbrowser.open(URL)
        return

    print(f"Starting Domestique on {URL}...")

    # Handle signals
    # CON5: ask uvicorn for a graceful shutdown (which drains in-flight
    # requests and runs the FastAPI lifespan teardown) instead of
    # sys.exit() killing the process immediately. Fall back to the old
    # behaviour if uvicorn hasn't been started yet (race on fast SIGINT).
    def signal_handler(sig, frame):
        print("\nShutting down...")
        _shutdown_event.set()
        # FIX26 (§7): release OS sleep/screensaver inhibit on exit so the
        # caffeinate / systemd-inhibit child doesn't outlive us.
        try:
            import sleep_inhibit
            sleep_inhibit.disable()
        except Exception:
            pass
        if _uvicorn_server is not None:
            _uvicorn_server.should_exit = True
            return
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal_handler)

    # Pinned-port guard: bail out NOW if 8080 is held by an unrelated process.
    # We refuse to silently float to a different port — single-instance
    # detection and any saved :8080 shortcuts would break (master decisions §3).
    _ensure_port_free_or_die()

    # Start server in background thread
    start_server()

    # Wait for server to respond, then open window
    server_up = wait_for_server()
    if server_up:
        print(f"Server ready → {URL}")
    else:
        log = _log()
        if _server_error is not None:
            msg = (
                f"Error: uvicorn thread crashed: "
                f"{type(_server_error).__name__}: {_server_error}"
            )
            print(msg)
            print("See ~/.domestique/logs/ for full traceback.")
            # v2.0.2 WIN-START-FIX: mirror to disk for windowed builds.
            if log is not None:
                log.error(msg)
            # v2.0.2 WIN-START-FIX: also dump the FULL captured traceback so
            # the cause is in the log file (and CI stdout), not just the type.
            if _server_traceback:
                print(_server_traceback)
                if log is not None:
                    log.error("uvicorn thread traceback:\n%s", _server_traceback)
            sys.exit(1)
        print("Warning: Server may not have started (timeout).")
        if log is not None:
            log.warning("Server may not have started (timeout after wait_for_server).")

    # v2.0.2 WIN-CI-SMOKE: headless server-only mode. A CI runner has no
    # display, so opening the pywebview window or the tray would block on a
    # GUI loop forever. Keep-alive instead so the smoke-test can poll
    # /api/version, then kill the process.
    #
    # v2.0.2 WIN-START-FIX: this mode must FAIL FAST + LOUD, never block
    # silently. The build is console=False, so a hung EXE looks identical to
    # a crashed one ("connection actively refused" for the full poll window).
    # BEFORE blocking we verify the server actually bound; if it did not
    # (wait_for_server() returned False / _server_error set), we write the
    # full traceback to the log AND print it, then sys.exit(1) so the EXE
    # exits non-zero with the cause instead of hanging. The whole path is
    # wrapped so ANY exception here is logged (full traceback) + printed +
    # exits 1. Non-server-only behavior below is untouched.
    if _is_server_only():
        log = _log()
        try:
            if not server_up:
                msg = (
                    "FATAL: server-only mode — server never came up "
                    f"(wait_for_server timed out / failed to bind {URL})."
                )
                print(msg)
                if log is not None:
                    log.error(msg)
                if _server_error is not None:
                    err = (
                        f"uvicorn thread crashed: "
                        f"{type(_server_error).__name__}: {_server_error}"
                    )
                    print(err)
                    if log is not None:
                        log.error(err)
                if _server_traceback:
                    print(_server_traceback)
                    if log is not None:
                        log.error("uvicorn thread traceback:\n%s", _server_traceback)
                sys.exit(1)

            print(f"server-only mode — serving on http://127.0.0.1:{PORT}")
            if log is not None:
                log.info("server-only mode — serving on http://127.0.0.1:%s", PORT)
            # Server confirmed up. Block until the process is killed (CI stops
            # it after the poll). _shutdown_event is also flipped by the SIGINT
            # handler, so Ctrl+C / SIGTERM still exits cleanly without a window.
            _shutdown_event.wait()
            return
        except SystemExit:
            # sys.exit(1) above is intentional — let it propagate.
            raise
        except Exception as e:
            # Any unexpected failure in the server-only path must surface
            # with a full traceback (windowed stdout is dead) and exit 1,
            # never leave the EXE hanging.
            import traceback
            tb = traceback.format_exc()
            print(f"FATAL: server-only mode crashed: {type(e).__name__}: {e}")
            print(tb)
            if log is not None:
                log.error("server-only mode crashed:\n%s", tb)
            sys.exit(1)

    # Try native window (pywebview), fall back to browser + tray
    try:
        import webview
        # v2.0.2 WIN-START-FIX: proactively import the platform backend
        # BEFORE webview.start(). On Windows the EdgeChromium/WinForms backend
        # bootstraps the .NET CLR via pythonnet; if that's missing the CLR
        # layer can hard-abort the process (not a catchable Python error),
        # which on a windowed build is the silent "nothing happens" death.
        # Importing the module here turns a missing backend into an ordinary
        # ImportError we can catch, engaging the browser fallback below.
        # macOS uses the Cocoa backend (no CLR), so its path is unchanged.
        import importlib
        if sys.platform == "win32":
            importlib.import_module("webview.platforms.edgechromium")
        # pywebview requires the main thread — skip pystray (tray not needed
        # when the app has its own window; closing the window exits the app)
        window = webview.create_window(
            "Domestique", URL,
            width=1400, height=900,
            min_size=(1000, 600),
            x=100, y=50,  # position near top-left, not bottom
            js_api=JsApi(),  # WKWebView ignores <a download>; JS calls
                             # window.pywebview.api.save_zwo/save_fit instead.
        )
        webview.start()
        print("Window closed — shutting down.")
        _shutdown_event.set()
        # FIX26 (§7): release OS sleep inhibit on normal window-close exit.
        try:
            import sleep_inhibit
            sleep_inhibit.disable()
        except Exception:
            pass
    except ImportError:
        _fallback_to_browser("pywebview/backend not available")
    except Exception as e:
        # WebView2 missing on Windows 10, or other pywebview error
        _fallback_to_browser(f"pywebview failed: {e}")


if __name__ == "__main__":
    main()
