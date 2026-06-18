"""v2.0.8 — B1 (Windows app won't reopen after close). On Windows the webview
CLR/WinForms backend + the bound :8080 socket lingered after webview.start()
returned, so the process didn't die; the next launch saw an orphan server with no
window and opened a browser tab instead of reopening the app ("kill it in Task
Manager every single time"). Fix: launcher._win_hard_exit() best-effort-stops
uvicorn then os._exit(0) on win32; macOS (Cocoa, no CLR) is a no-op.

The Windows runtime path can't be exercised on macOS; these assert the helper's
logic with os._exit mocked.
"""
import unittest
from unittest.mock import MagicMock, patch


class TestWinHardExit(unittest.TestCase):
    def test_win32_stops_uvicorn_and_hard_exits(self):
        import launcher
        fake_server = MagicMock()
        with patch.object(launcher, "_uvicorn_server", fake_server), \
             patch.object(launcher.os, "_exit") as mock_exit:
            launcher._win_hard_exit(platform="win32")
            self.assertTrue(fake_server.should_exit, "must signal uvicorn to stop")
            mock_exit.assert_called_once_with(0)

    def test_win32_hard_exits_even_without_server(self):
        import launcher
        with patch.object(launcher, "_uvicorn_server", None), \
             patch.object(launcher.os, "_exit") as mock_exit:
            launcher._win_hard_exit(platform="win32")
            mock_exit.assert_called_once_with(0)

    def test_macos_is_a_noop(self):
        import launcher
        with patch.object(launcher.os, "_exit") as mock_exit:
            self.assertFalse(launcher._win_hard_exit(platform="darwin"))
            mock_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
