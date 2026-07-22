"""v3.5.2 — cooldowns must descend, in the file itself.

Owner rode vo2max_ou_53min.zwo: the modal drew the cooldown descending (the
renderer force-slopes Cooldown down) but the ZWO authored it ascending
(PowerLow=0.25 → PowerHigh=0.75), and trainer apps that play attributes
chronologically (MyWhoosh / Tacx) ramped him UP to 75% FTP as a "cooldown".
776 files carried the pattern (the v2.4.0 warmup-migration copied the Warmup
low→high shape into cooldowns); the library majority (2,873) was already
authored chronologically descending. All 776 were normalized to the majority
convention: trailing <Cooldown PowerLow=HIGH PowerHigh=LOW>, description
text swapped to match.

This test makes the invariant permanent: a trailing Cooldown may never ramp
up again, so what the chart shows, what the FIT plays, and what a
chronological ZWO player drives are the same ride.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

WORKOUTS = Path(__file__).resolve().parent.parent / "workouts"


def _trailing_cooldowns():
    for p in sorted(WORKOUTS.glob("*.zwo")):
        try:
            w = ET.parse(p).getroot().find("workout")
        except (ET.ParseError, OSError):
            continue
        if w is None or not len(w):
            continue
        last = list(w)[-1]
        if last.tag == "Cooldown":
            yield p.name, float(last.get("PowerLow", 0)), float(last.get("PowerHigh", 0))


def test_no_trailing_cooldown_ramps_up():
    offenders = [(n, lo, hi) for n, lo, hi in _trailing_cooldowns() if hi > lo]
    assert offenders == [], (
        f"{len(offenders)} trailing cooldowns ramp UP (chronological players "
        f"drive the rider harder at the end): {offenders[:8]}")


def test_owner_reported_file_descends():
    # The file from the report, pinned end-to-end: attrs descend and the
    # description narrates the same direction.
    p = WORKOUTS / "vo2max_ou_53min.zwo"
    root = ET.parse(p).getroot()
    last = list(root.find("workout"))[-1]
    assert last.tag == "Cooldown"
    assert float(last.get("PowerLow")) == 0.75
    assert float(last.get("PowerHigh")) == 0.25
    assert "Cooldown: 8min from 75% to 25% FTP" in (root.findtext("description") or "")
