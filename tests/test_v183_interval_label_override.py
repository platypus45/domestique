"""v1.8.3 BUG-E — interval label override for misclassified RECOVERY rows.

User screenshot of the 23 INTERVALS table showed many rows labelled
"RECOVERY" while Avg Power was 189-297W (Z3-Z5+ on a 248W FTP).
ICU's auto-detection labels long flat segments "RECOVERY" regardless
of actual power — Domestique used to display the label verbatim.

These tests pin down ``_display_interval_name`` (the helper that
backs ``_project_intervals_for_display``) added to ``app.py``:

* RECOVERY + Z2+ avg_power → "Z<n> <watts>W" override.
* RECOVERY + Z1 avg_power → keep ICU's label (genuine recovery).
* Structured group_id labels (e.g. "302s@243w91rpm") → preserved.
* Missing FTP → fall back to ICU's name.
* Missing avg_power → fall back to ICU's name.
"""
from __future__ import annotations

import unittest

from app import _display_interval_name


class TestIntervalLabelOverride(unittest.TestCase):
    """The new RECOVERY-label override + its escape hatches."""

    def test_recovery_label_with_z3_power_is_overridden(self):
        # The exact bug from the screenshot: ICU calls the segment
        # "RECOVERY" but avg_power = 189W at FTP 248 = 76.2% = Z3 Tempo.
        # Expected display: "Z3 189W".
        row = {"name": "RECOVERY", "avg_power_w": 189}
        self.assertEqual(
            _display_interval_name(row, ftp=248),
            "Z3 189W",
        )

    def test_recovery_label_with_z1_power_is_preserved(self):
        # 120W at FTP 248 = 48.4% = Z1 — a real recovery segment.
        # ICU's "RECOVERY" label is accurate; do not override.
        row = {"name": "RECOVERY", "avg_power_w": 120}
        self.assertEqual(
            _display_interval_name(row, ftp=248),
            "RECOVERY",
        )

    def test_structured_icu_name_is_always_preserved(self):
        # ICU's group_id encoding "<seconds>s@<watts>w<cadence>rpm" is
        # the most informative form — never override, even when the
        # avg_power would put it elsewhere.
        row = {"name": "302s@243w91rpm", "avg_power_w": 243}
        self.assertEqual(
            _display_interval_name(row, ftp=248),
            "302s@243w91rpm",
        )

    def test_missing_ftp_falls_back_to_icu_name(self):
        # No FTP configured → can't compute %FTP → no override.
        # Return ICU's raw name verbatim.
        row = {"name": "RECOVERY", "avg_power_w": 189}
        self.assertEqual(
            _display_interval_name(row, ftp=None),
            "RECOVERY",
        )

    def test_missing_avg_power_falls_back_to_icu_name(self):
        # No avg_power_w on the row (older ICU sync) → no override.
        row = {"name": "RECOVERY", "avg_power_w": None}
        self.assertEqual(
            _display_interval_name(row, ftp=248),
            "RECOVERY",
        )


if __name__ == "__main__":
    unittest.main()
