"""Smoke tests for sleep_inhibit — platform-branch behaviour.

Each test patches platform.system() and the relevant subprocess / ctypes
bindings, so the suite runs green on any host regardless of OS.

FIX26 §7: keeps the machine awake during a ride; released on stop / exit.
"""
from unittest.mock import patch, MagicMock
import importlib
import pytest


def _fresh_module():
    """Re-import sleep_inhibit so each test starts with clean globals."""
    import sleep_inhibit
    # Ensure a fresh module-level state between tests (the real module holds
    # _proc / _win_state_prev globals; reset them explicitly rather than
    # rely on reload ordering).
    sleep_inhibit._proc = None
    sleep_inhibit._win_state_prev = None
    return sleep_inhibit


def test_sleep_inhibit_macos_spawns_caffeinate():
    """Darwin branch spawns `caffeinate -i -w <pid>`."""
    si = _fresh_module()
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll.return_value = None
    with patch("sleep_inhibit.platform.system", return_value="Darwin"), \
         patch("sleep_inhibit.subprocess.Popen", return_value=fake_proc) as popen, \
         patch("sleep_inhibit.os.getpid", return_value=999):
        ok = si.enable()
    assert ok is True
    popen.assert_called_once()
    cmd = popen.call_args[0][0]
    assert cmd[0] == "caffeinate"
    assert "-i" in cmd
    assert "-w" in cmd
    assert "999" in cmd
    # Cleanup so module globals don't leak into other tests.
    si._proc = None


def test_sleep_inhibit_windows_sets_exec_state():
    """Windows branch calls SetThreadExecutionState with the expected flag set."""
    si = _fresh_module()
    fake_ctypes = MagicMock()
    fake_ctypes.windll.kernel32.SetThreadExecutionState.return_value = 0x1
    with patch("sleep_inhibit.platform.system", return_value="Windows"), \
         patch.dict("sys.modules", {"ctypes": fake_ctypes}):
        ok = si.enable()
    assert ok is True
    fake_ctypes.windll.kernel32.SetThreadExecutionState.assert_called_once()
    flags = fake_ctypes.windll.kernel32.SetThreadExecutionState.call_args[0][0]
    # 0x80000000 | 0x1 | 0x2 = 0x80000003 — require all three bits set.
    assert flags & 0x80000000  # ES_CONTINUOUS
    assert flags & 0x00000001  # ES_SYSTEM_REQUIRED
    assert flags & 0x00000002  # ES_DISPLAY_REQUIRED
    si._win_state_prev = None


def test_sleep_inhibit_linux_systemd():
    """Linux branch spawns systemd-inhibit with --what=idle --mode=block."""
    si = _fresh_module()
    fake_proc = MagicMock()
    fake_proc.pid = 54321
    fake_proc.poll.return_value = None
    with patch("sleep_inhibit.platform.system", return_value="Linux"), \
         patch("sleep_inhibit.subprocess.Popen", return_value=fake_proc) as popen:
        ok = si.enable()
    assert ok is True
    cmd = popen.call_args[0][0]
    assert cmd[0] == "systemd-inhibit"
    assert "--what=idle" in cmd
    assert "--mode=block" in cmd
    si._proc = None


def test_disable_is_idempotent():
    """disable() must be safe to call when nothing is inhibited + after terminate."""
    si = _fresh_module()
    # First call: no state, no crash.
    si.disable()
    # Second call: still no state, still safe.
    si.disable()
    # Now simulate a live process, then disable twice.
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    si._proc = fake_proc
    si.disable()
    assert si._proc is None
    si.disable()  # second call must be a no-op


def test_enable_twice_reuses_process():
    """Second enable() on Darwin/Linux reuses the existing live subprocess."""
    si = _fresh_module()
    fake_proc = MagicMock()
    fake_proc.pid = 11111
    fake_proc.poll.return_value = None   # still alive
    si._proc = fake_proc
    with patch("sleep_inhibit.platform.system", return_value="Darwin"), \
         patch("sleep_inhibit.subprocess.Popen") as popen:
        ok = si.enable()
    assert ok is True
    popen.assert_not_called()   # did NOT spawn a second caffeinate
    si._proc = None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
