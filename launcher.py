#!/usr/bin/env python3
"""
Domestique Launcher — entry point for the packaged desktop app.

Starts the FastAPI server IN-PROCESS (not as subprocess, to avoid PyInstaller
fork issues), opens the browser, and shows a system tray icon.

Works both in dev mode (python launcher.py) and frozen mode (PyInstaller).
"""

import multiprocessing
import os
import sys
import threading
import time
import webbrowser
import signal

# Prevent PyInstaller frozen multiprocessing fork bomb
multiprocessing.freeze_support()

# Port 8080 is PINNED for single-instance hygiene: the tray icon + existing
# instance guard both assume localhost:8080. If we let uvicorn float the
# port, the single-instance detection breaks and desktop shortcuts that
# point at :8080 stop working across restarts.
# (Master decisions §3 — fail loudly if 8080 is busy.)
PORT = 8080
URL = f"http://localhost:{PORT}"


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
        print(
            f"\nFATAL: cannot bind 127.0.0.1:{PORT} ({e}).\n"
            f"Domestique requires port {PORT} for single-instance detection. "
            f"Stop the conflicting process and try again.\n"
        )
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
        global _server_error, _uvicorn_server
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
            # confusing "server didn't start" with no traceback. Log
            # here and expose the error flag so the main thread can
            # surface it.
            import traceback
            _server_error = e
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
    if wait_for_server():
        print(f"Server ready → {URL}")
    else:
        if _server_error is not None:
            print(
                f"Error: uvicorn thread crashed: "
                f"{type(_server_error).__name__}: {_server_error}"
            )
            print("See ~/.domestique/logs/ for full traceback.")
            sys.exit(1)
        print("Warning: Server may not have started (timeout).")

    # Try native window (pywebview), fall back to browser + tray
    try:
        import webview
        # pywebview requires the main thread — skip pystray (tray not needed
        # when the app has its own window; closing the window exits the app)
        window = webview.create_window(
            "Domestique", URL,
            width=1400, height=900,
            min_size=(1000, 600),
            x=100, y=50,  # position near top-left, not bottom
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
        print("(pywebview not installed — opening in browser)")
        webbrowser.open(URL)
        run_with_tray()
    except Exception as e:
        # WebView2 missing on Windows 10, or other pywebview error
        print(f"(pywebview failed: {e} — opening in browser)")
        webbrowser.open(URL)
        run_with_tray()


if __name__ == "__main__":
    main()
