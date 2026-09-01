"""Cross-OS sleep inhibit — keeps the machine awake during active rides.

macOS: caffeinate -i subprocess
Windows: SetThreadExecutionState flags
Linux: systemd-inhibit subprocess
"""
import platform
import subprocess
import os
import logging

log = logging.getLogger(__name__)

_proc = None   # macOS / Linux subprocess
_win_state_prev = None   # Windows


def enable() -> bool:
    """Start inhibiting sleep. Idempotent — calling twice is safe."""
    global _proc, _win_state_prev
    sys = platform.system()
    if sys == "Darwin":
        if _proc and _proc.poll() is None:
            return True
        try:
            _proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            log.info("sleep_inhibit: caffeinate pid=%d", _proc.pid)
            return True
        except FileNotFoundError:
            log.warning("sleep_inhibit: caffeinate not found on macOS")
            return False
    elif sys == "Windows":
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            _win_state_prev = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            log.info("sleep_inhibit: Windows SetThreadExecutionState")
            return True
        except Exception as e:
            log.warning("sleep_inhibit Windows failed: %s", e)
            return False
    elif sys == "Linux":
        if _proc and _proc.poll() is None:
            return True
        try:
            _proc = subprocess.Popen([
                "systemd-inhibit", "--what=idle",
                "--mode=block", "--why=Domestique ride in progress",
                "sleep", "infinity",
            ])
            log.info("sleep_inhibit: systemd-inhibit pid=%d", _proc.pid)
            return True
        except FileNotFoundError:
            log.warning("sleep_inhibit: systemd-inhibit not found on Linux")
            return False
    return False


def disable() -> None:
    """Release sleep inhibit. Idempotent."""
    global _proc, _win_state_prev
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    if _win_state_prev is not None:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
        _win_state_prev = None
    log.info("sleep_inhibit: released")
