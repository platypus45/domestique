"""Tests for gpx_to_gc.py — GPX → CRS conversion edge cases.

v3.6.0-fix28 L-7: first-point missing <ele> warning.
"""

import logging
import tempfile
from pathlib import Path

import pytest

from gpx_to_gc import parse_gpx


class TestFix28L7FirstEleWarn:
    """First track point missing <ele> must emit a distinctive WARNING.

    Before fix28, the generic per-point warning fired but didn't highlight
    that the entire elevation series was anchored to 0.0m (subsequent fill
    via `last_valid_ele` = 0.0). The fix distinguishes idx==0 with an
    anchor-specific message.
    """

    def _make_gpx_missing_first_ele(self) -> Path:
        """GPX with no <ele> on the first trkpt, real ele on the second."""
        gpx = (
            '<?xml version="1.0"?>'
            '<gpx xmlns="http://www.topografix.com/GPX/1/1">'
            '<trk><trkseg>'
            '<trkpt lat="45.0" lon="6.0"></trkpt>'  # no <ele>
            '<trkpt lat="45.001" lon="6.001"><ele>1500.0</ele></trkpt>'
            '<trkpt lat="45.002" lon="6.002"><ele>1510.0</ele></trkpt>'
            '</trkseg></trk>'
            '</gpx>'
        )
        f = Path(tempfile.mkdtemp()) / "missing_first.gpx"
        f.write_text(gpx, encoding="utf-8")
        return f

    def test_first_ele_missing_emits_anchor_warning(self, caplog):
        path = self._make_gpx_missing_first_ele()
        caplog.set_level(logging.WARNING)
        pts = parse_gpx(path)
        assert len(pts) == 3

        # First point gets anchored to 0.0 (the `last_valid_ele` starting
        # value); later points use their real <ele>.
        assert pts[0]["ele"] == 0.0
        assert pts[1]["ele"] == 1500.0

        anchor_msgs = [
            r.getMessage() for r in caplog.records
            if "anchoring elevation series to 0.0m" in r.getMessage()
        ]
        assert anchor_msgs, (
            f"expected anchor-specific warning for first-point missing <ele>; "
            f"saw: {[r.getMessage() for r in caplog.records]}"
        )
