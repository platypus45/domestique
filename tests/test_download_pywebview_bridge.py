"""v1.0.3 regression tests for the pywebview JS-API download bridge.

Pre-fix the dashboard "Download ZWO" / "Download FIT" buttons used
``<a download>`` (ZWO) and synthetic anchor clicks (FIT). WKWebView
(pywebview's macOS backend) silently ignores both, so on the packaged
DMG the user saw the ZWO file rendered inline as text and the FIT
button did nothing.

The fix exposes a small JS-callable bridge from ``launcher.JsApi`` that
opens a native save dialog and writes the file. These tests exercise
the Python side of the bridge with ``webview.windows[0].create_file_dialog``
monkeypatched — they do NOT spin up a real native window.
"""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# Ensure the repo root is on sys.path so ``import launcher`` works when
# the tests run from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _BridgeTestCase(unittest.TestCase):
    """Shared scaffolding: import launcher and stub
    ``webview.windows[0].create_file_dialog`` per-test.

    ``launcher.JsApi._save`` does ``import webview`` inside the method,
    which is satisfied by the cached module in ``sys.modules`` — so we
    replace the live module's ``windows`` list with one fake window and
    monkey-patch ``create_file_dialog`` on it.
    """

    def _install_window(self, dialog_return):
        import webview  # the real module

        fake_window = mock.MagicMock()
        fake_window.create_file_dialog.return_value = dialog_return
        # Save and restore the original windows list so tests don't leak.
        self._orig_windows = webview.windows
        webview.windows = [fake_window]
        self.addCleanup(self._restore_windows, webview)
        return fake_window

    def _restore_windows(self, webview_mod):
        webview_mod.windows = self._orig_windows


class TestSaveZwo(_BridgeTestCase):
    def test_save_zwo_writes_file_with_given_content(self):
        import launcher

        d = tempfile.mkdtemp(prefix="domestique-bridge-")
        target = str(Path(d) / "out.zwo"  )
        fake_window = self._install_window((target,))

        api = launcher.JsApi()
        res = api.save_zwo("planned.zwo", "<workout_file/>")

        self.assertTrue(res["ok"], f"unexpected error: {res}")
        self.assertEqual(res["path"], target)
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "<workout_file/>")
        # Dialog was seeded with the suggested filename.
        kwargs = fake_window.create_file_dialog.call_args.kwargs
        self.assertEqual(kwargs.get("save_filename"), "planned.zwo")

    def test_save_zwo_cancelled_returns_error_cancelled(self):
        import launcher

        # pywebview returns None when the user cancels the save sheet.
        self._install_window(None)
        api = launcher.JsApi()
        res = api.save_zwo("planned.zwo", "<workout_file/>")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cancelled")


class TestSaveFit(_BridgeTestCase):
    def test_save_fit_decodes_base64_and_writes_bytes(self):
        import launcher

        payload = b"\x0e\x10iMFIT\x00\x01\x02\x03"  # FIT-ish bytes
        b64 = base64.b64encode(payload).decode("ascii")
        d = tempfile.mkdtemp(prefix="domestique-bridge-")
        target = str(Path(d) / "out.fit")
        self._install_window((target,))

        api = launcher.JsApi()
        res = api.save_fit("workout.fit", b64)

        self.assertTrue(res["ok"], f"unexpected error: {res}")
        self.assertEqual(Path(target).read_bytes(), payload)

    def test_save_fit_malformed_base64_returns_error(self):
        import launcher

        # No need for a window — decode fails first.
        self._install_window((str(Path(tempfile.mkdtemp()) / "x.fit"),))
        api = launcher.JsApi()
        res = api.save_fit("workout.fit", "###not-base64###")
        self.assertFalse(res["ok"])
        self.assertIn("base64", res["error"].lower())



class TestSaveCrs(_BridgeTestCase):
    """Course downloads did nothing on Windows and Linux: the buttons used
    <a download> and window.open(), which the app window never turns into a
    save. They now go through the same bridge as the workouts."""

    def test_save_crs_writes_the_course_text(self):
        import launcher

        target = str(Path(tempfile.mkdtemp(prefix="domestique-bridge-")) / "out.crs")
        fake_window = self._install_window((target,))
        res = launcher.JsApi().save_crs("Alto Arrate.crs", "[COURSE HEADER]\n", "basque")
        self.assertTrue(res["ok"], f"unexpected error: {res}")
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "[COURSE HEADER]\n")
        kwargs = fake_window.create_file_dialog.call_args.kwargs
        self.assertEqual(kwargs.get("save_filename"), "Alto Arrate.crs")

    def test_an_empty_body_is_read_off_disk(self):
        import app
        import launcher

        src = Path(tempfile.mkdtemp(prefix="domestique-crs-")) / "c.crs"
        src.write_bytes(b"[COURSE DATA]\n")
        target = str(src.parent / "saved.crs")
        self._install_window((target,))
        with mock.patch.object(app, "course_file_path", return_value=src) as cfp:
            res = launcher.JsApi().save_crs("c.crs", "", "basque")
        self.assertTrue(res["ok"], f"unexpected error: {res}")
        cfp.assert_called_once_with("basque", "c.crs")
        self.assertEqual(Path(target).read_bytes(), b"[COURSE DATA]\n")

    def test_both_course_buttons_use_the_bridge(self):
        html = (REPO_ROOT / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("/download\" class=\"btn btn-primary\" download>Download CRS", html)
        self.assertNotIn("window.open(`/api/course/", html)
        self.assertIn("onclick=\"downloadCrsFile(this.dataset.region, this.dataset.file)\"", html)
        self.assertIn("await downloadCrsFile(region, filename);", html)
        self.assertIn("window.pywebview.api.save_crs(", html)

if __name__ == "__main__":
    unittest.main()
