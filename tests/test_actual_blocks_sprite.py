"""Completed grid days carry the ride's own lap blocks, not just plan data.

Reported with a side-by-side: intervals.icu's calendar card draws the ride
you actually did; Domestique's grid kept showing the planned silhouette on
ridden days — "a standard preview, completely different from the blocks I
did". The lap timeline stored on every synced ride already carries the real
structure, so the actual payload now ships it render-ready.
"""
from __future__ import annotations

import app as app_module


def _ride(laps, ftp_at_ride=250):
    return {"ride_id": "icu_x", "source": "icu", "started_at": "2026-08-30T11:43:32",
            "duration_s": 3600, "tss": 76, "avg_power_w": 216,
            "ftp_at_ride": ftp_at_ride, "intervals": laps}


def test_laps_become_sprite_blocks():
    laps = [
        {"type": "RECOVERY", "start_s": 0, "duration_s": 729, "avg_power_w": 160},
        {"type": "WORK", "start_s": 729, "duration_s": 58, "avg_power_w": 264},
        {"type": "WORK", "start_s": 988, "duration_s": 19, "avg_power_w": 467},
    ]
    blocks = app_module._actual_blocks_from_laps(_ride(laps), ftp=248)
    assert len(blocks) == 3
    # Shape is exactly what miniPowerBlockSVG eats.
    assert set(blocks[0]) == {"min", "pctLow", "pctHigh"}
    # ftp_at_ride (250) wins over the profile ftp argument.
    assert blocks[2]["pctLow"] == round(467 / 250 * 100, 1)
    assert blocks[0]["min"] == round(729 / 60, 2)


def test_no_laps_or_no_ftp_means_no_blocks():
    assert app_module._actual_blocks_from_laps(_ride([]), ftp=248) == []
    r = _ride([{"duration_s": 60, "avg_power_w": 200}], ftp_at_ride=None)
    assert app_module._actual_blocks_from_laps(r, ftp=None) == []


def test_degenerate_per_second_laps_are_refused():
    """121+ laps is a device exporting a lap per second, not a structure —
    rendering it would draw noise and the planned silhouette is honester."""
    laps = [{"duration_s": 1, "avg_power_w": 200} for _ in range(150)]
    assert app_module._actual_blocks_from_laps(_ride(laps), ftp=248) == []


def test_summarizer_carries_blocks():
    laps = [{"duration_s": 300, "avg_power_w": 200}]
    payload = app_module._summarize_ride_for_calendar(_ride(laps), ftp=248)
    assert payload["blocks"] and payload["blocks"][0]["pctLow"] == 80.0
