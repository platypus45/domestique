"""v1.0.1 regression tests for the planner-modal "Download ZWO" / "Download FIT"
buttons.

Bug 1 (ZWO 404) — pre-fix, the dashboard called
``/api/download/zwo/<filename>`` (one segment) but only the two-segment route
``/api/download/zwo/<category>/<filename>`` was registered. FastAPI returned
404 with ``{"error": "not found"}`` and the user saw a silent failure
(``<a download>`` followed by JSON 404).

Bug 2 (FIT silent fail / wrong workout) — the FIT export endpoint matched
``session_type`` literally with ``==``. The dashboard <select> options use
the snake_case form (``sweet_spot``, ``over_under``) while the planner uses
the canonical form (``sweetspot``, ``overunder``). Pre-fix, the snake_case
forms fell through to the else branch (a generic Z2 block), silently
producing a wrong workout instead of the requested intervals.

Acceptance gates these tests cover:
- ``/api/download/zwo/<existing-file>.zwo`` returns 200 + ``application/xml``
- ``/api/download/zwo/<cat>/<existing-file>.zwo`` still works (back-compat)
- ``/api/export/fit-workout?session_type=z2`` returns 200 + binary FIT
- All four canonical session types (z2, sweet_spot, vo2max, threshold) +
  their snake_case aliases produce valid binary, NOT JSON 500.
- The sweetspot / overunder aliases produce the SAME bytes whether spelled
  as ``sweetspot``/``overunder`` (planner form) or ``sweet_spot``/
  ``over_under`` (dashboard form).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = REPO_ROOT / "workouts"


def _pick_zwo() -> str:
    """Return a real .zwo filename present in the repo so the test isn't
    coupled to any one workout. Skips the README.md sibling."""
    for p in sorted(WORKOUTS_DIR.glob("*.zwo")):
        return p.name
    raise RuntimeError(f"no .zwo files in {WORKOUTS_DIR}")


class TestZwoDownloadRoutes(unittest.TestCase):
    """v1.0.1 Bug 1 — single-arg route variant must coexist with the
    pre-existing two-arg route."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        self.zwo_name = _pick_zwo()

    def test_single_arg_route_returns_xml(self):
        r = self.client.get(f"/api/download/zwo/{self.zwo_name}")
        self.assertEqual(r.status_code, 200)
        # v1.6.4: media_type changed from "application/xml" to
        # "application/octet-stream" because WKWebView (packaged DMG)
        # rendered XML inline instead of triggering save. Body is still
        # the raw ZWO XML — only the wire framing changed.
        self.assertTrue(
            r.headers["content-type"].startswith("application/octet-stream"),
            f"unexpected content-type: {r.headers['content-type']!r}",
        )
        # Filename is in the Content-Disposition header
        cd = r.headers.get("content-disposition", "")
        self.assertIn(self.zwo_name, cd)
        # Body looks like a ZWO file
        self.assertTrue(r.content.startswith(b"<"), f"body not XML-ish: {r.content[:80]!r}")

    def test_two_arg_route_still_works(self):
        # any category prefix — the route falls back to flat layout if not found
        r = self.client.get(f"/api/download/zwo/anycat/{self.zwo_name}")
        self.assertEqual(r.status_code, 200)
        # v1.6.4: media_type changed to "application/octet-stream" (see
        # test_single_arg_route_returns_xml above for context).
        self.assertTrue(
            r.headers["content-type"].startswith("application/octet-stream"),
            f"unexpected content-type: {r.headers['content-type']!r}",
        )
        cd = r.headers.get("content-disposition", "")
        self.assertIn(self.zwo_name, cd)

    def test_single_arg_404_for_missing(self):
        r = self.client.get("/api/download/zwo/does_not_exist_xyz.zwo")
        self.assertEqual(r.status_code, 404)


class TestFitExportEndpoint(unittest.TestCase):
    """v1.0.1 Bug 2 — endpoint must return binary FIT (not 500 JSON) and
    must normalise session_type so dashboard snake_case forms work."""

    def setUp(self):
        self.client = TestClient(app_module.app)

    def _assert_binary_fit(self, r):
        self.assertEqual(r.status_code, 200, f"got {r.status_code}: {r.content[:200]!r}")
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        cd = r.headers.get("content-disposition", "")
        self.assertIn(".fit", cd)
        # FIT file header — first byte is the header size (12 or 14).
        # If it were a JSON error we'd see ord('{') = 0x7b.
        self.assertNotEqual(r.content[:1], b"{", "endpoint returned JSON, not FIT binary")
        self.assertGreater(len(r.content), 50, "FIT body suspiciously small")

    def test_z2_returns_binary(self):
        r = self.client.get("/api/export/fit-workout?session_type=z2&duration_min=60&name=test")
        self._assert_binary_fit(r)

    def test_threshold_returns_binary(self):
        r = self.client.get("/api/export/fit-workout?session_type=threshold&duration_min=60&name=test")
        self._assert_binary_fit(r)

    def test_vo2max_returns_binary(self):
        r = self.client.get("/api/export/fit-workout?session_type=vo2max&duration_min=60&name=test")
        self._assert_binary_fit(r)

    def test_sweetspot_canonical_returns_binary(self):
        # planner form
        r = self.client.get("/api/export/fit-workout?session_type=sweetspot&duration_min=60&name=test")
        self._assert_binary_fit(r)

    def test_sweet_spot_alias_returns_same_workout_shape(self):
        # dashboard <select> form — pre-fix this fell through to else (Z2 block)
        # and produced a tiny FIT (just warmup + Z2 + cooldown). After the
        # normalise fix it should produce the same shape as 'sweetspot'.
        r_canonical = self.client.get(
            "/api/export/fit-workout?session_type=sweetspot&duration_min=60&name=test"
        )
        r_alias = self.client.get(
            "/api/export/fit-workout?session_type=sweet_spot&duration_min=60&name=test"
        )
        self._assert_binary_fit(r_canonical)
        self._assert_binary_fit(r_alias)
        # Same number of workout steps → same body length (FIT step records are
        # fixed-size). Pre-fix, the alias produced a much smaller file because
        # it skipped the 3 sweet-spot intervals.
        self.assertEqual(
            len(r_canonical.content), len(r_alias.content),
            "sweet_spot alias produced different byte count than sweetspot — "
            "session_type normalisation regressed",
        )

    def test_overunder_alias_returns_same_workout_shape(self):
        r_canonical = self.client.get(
            "/api/export/fit-workout?session_type=overunder&duration_min=60&name=test"
        )
        r_alias = self.client.get(
            "/api/export/fit-workout?session_type=over_under&duration_min=60&name=test"
        )
        self._assert_binary_fit(r_canonical)
        self._assert_binary_fit(r_alias)
        self.assertEqual(
            len(r_canonical.content), len(r_alias.content),
            "over_under alias produced different byte count than overunder",
        )

    def test_uppercase_input_normalises(self):
        # Defensive: anyone calling the API directly might send "VO2MAX".
        r = self.client.get("/api/export/fit-workout?session_type=VO2MAX&duration_min=60&name=test")
        self._assert_binary_fit(r)


if __name__ == "__main__":
    unittest.main()
