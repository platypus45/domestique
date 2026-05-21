"""v1.8.8 Bug 4 — course profile Unicode-normalization fallback.

Master decisions §Bug 4: ``/api/course/{region}/{filename}`` retries
NFC / NFD on 404 so cached routes.json from before the v1.8.6 ASCII
rename still loads when the disk file is in a different normalization
form.
"""
from __future__ import annotations

import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


_MIN_CRS = """[COURSE HEADER]
NAME = Test
[END COURSE HEADER]
[COURSE DATA]
0.0 0.0
1.0 5.0
[END COURSE DATA]
"""


class TestCourseUnicodeFallback(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        # Mirror the courses layout: <COURSE_DIR>/<region>/<filename>
        self._region_dir = self._tmp / "test_region"
        self._region_dir.mkdir(parents=True)
        self._patch_dir = patch.object(app_module, "COURSE_DIR", self._tmp)
        self._patch_dir.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_dir.stop()
        self._tmpdir.cleanup()

    def test_nfc_form_on_disk_resolves_when_nfd_requested(self):
        """Disk holds NFC ``pavé.crs``; client asks for the NFD form."""
        nfc_name = unicodedata.normalize("NFC", "pavé.crs")
        nfd_name = unicodedata.normalize("NFD", "pavé.crs")
        # Sanity: the two normalizations differ as strings.
        self.assertNotEqual(nfc_name, nfd_name)
        (self._region_dir / nfc_name).write_text(_MIN_CRS, encoding="utf-8")
        r = self.client.get(f"/api/course/test_region/{nfd_name}")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("points", data)
        self.assertGreaterEqual(len(data["points"]), 1)

    def test_404_for_truly_missing_file_logs_attempts(self):
        """An unknown filename still 404s and logs the attempted variants."""
        with self.assertLogs(app_module._log, level="WARNING") as cm:
            r = self.client.get("/api/course/test_region/nope.crs")
        self.assertEqual(r.status_code, 404, r.text)
        joined = "\n".join(cm.output)
        self.assertIn("nope.crs", joined)


if __name__ == "__main__":
    unittest.main()
