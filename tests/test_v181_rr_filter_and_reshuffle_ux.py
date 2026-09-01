"""v1.8.1 — RR sentinel filter + staged reshuffle progress UX.

User wore HR strap, did a ride; FIT carried 5658 HrvMessage records
(28290 RR slots) — but 80% were the FIT-spec sentinel ``65.535 s``
(=0xFFFF / 1000) for unused RR positions in each 5-slot HrvMessage.

Pre-v1.8.1 ``parse_rr_intervals`` filter was ``v > 0``, so the sentinels
made it into the RR array. DFA saw an array dominated by 65.535 values
and bailed with ``no_rr_data`` — the entire DFA pipeline silently
produced nothing for every chest-strap ride.

v1.8.1 narrows the filter to ``0.25 < v < 3.0`` (realistic 30-200 bpm
RR window). On the user's actual ride this raised the valid-RR count
from 0 to 13386 and DFA computed ``avg=1.02, lt1_minutes=4``.

Also: reshuffle accept flow now paints a staged progress display
(WORKOUT RESHUFFLED → UPDATING PLAN → UPDATING CALENDAR → DONE) so the
multi-second post-accept refresh is legible.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fit_activity


class HrvMessage:
    """Stub matching ``type(msg).__name__ == "HrvMessage"`` check in
    fit_activity.parse_rr_intervals."""
    def __init__(self, slots: list[float]):
        self.time = slots


class _MockRecord:
    def __init__(self, message):
        self.message = message


class _MockFitFile:
    def __init__(self, records):
        self.records = records


def _patch_fit_file(records):
    """Patch fit_tool.fit_file.FitFile.from_file so parse_rr_intervals
    sees the given record list without hitting disk."""
    class _Loader:
        @staticmethod
        def from_file(_path):
            return _MockFitFile(records)
    return patch("fit_tool.fit_file.FitFile", _Loader)


def _make_record(slots):
    return _MockRecord(HrvMessage(slots))


def test_rr_filter_drops_FIT_sentinel():
    """0xFFFF / 1000 = 65.535 s sentinels must be filtered out so DFA
    sees only realistic RR intervals."""
    # Realistic NSR pattern: 750 ms beats ± 50 ms jitter (80 bpm).
    valid_rrs = [0.750, 0.730, 0.770, 0.745]
    # FIT HrvMessage carries up to 5 slots, padding the rest with 65.535
    # (0xFFFF / 1000). Build mixed slots.
    SENTINEL = 65.535
    slots = [valid_rrs[0], SENTINEL, valid_rrs[1], SENTINEL, SENTINEL]
    rec = _make_record(slots)
    with _patch_fit_file([rec]):
        rrs = fit_activity.parse_rr_intervals(Path("/tmp/x.fit"))
    assert SENTINEL not in rrs
    assert len(rrs) == 2  # only the 2 valid slots
    assert all(0.25 < v < 3.0 for v in rrs)


def test_rr_filter_keeps_realistic_range():
    """Realistic 30-200 bpm RR (0.3-2.0 s) must pass through."""
    valid_set = [0.300, 0.500, 0.750, 1.200, 2.000]
    slots = valid_set + [65.535]  # 5 valid + 1 sentinel
    rec = _make_record(slots)
    with _patch_fit_file([rec]):
        rrs = fit_activity.parse_rr_intervals(Path("/tmp/x.fit"))
    assert sorted(rrs) == sorted(valid_set)


def test_rr_filter_drops_zero_and_negative():
    """Defensive: 0.0 and negative values are filtered too."""
    slots = [0.0, -0.1, 0.700, 65.535, 0.800]
    rec = _make_record(slots)
    with _patch_fit_file([rec]):
        rrs = fit_activity.parse_rr_intervals(Path("/tmp/x.fit"))
    assert 0.0 not in rrs
    assert all(v > 0 for v in rrs)
    assert sorted(rrs) == [0.700, 0.800]


def test_rr_filter_drops_above_3s():
    """Pathological large values (>3 s = <20 bpm) get filtered.

    Includes the FIT-spec 65.535 sentinel and any other sensor glitches
    that produce out-of-range RR durations."""
    slots = [0.500, 3.5, 65.535, 100.0, 0.700]
    rec = _make_record(slots)
    with _patch_fit_file([rec]):
        rrs = fit_activity.parse_rr_intervals(Path("/tmp/x.fit"))
    assert sorted(rrs) == [0.500, 0.700]


def test_dashboard_reshuffle_accept_paints_staged_progress():
    """v1.8.1 progress UX: accept flow paints 4 stages (WORKOUT
    RESHUFFLED → UPDATING PLAN → UPDATING CALENDAR → DONE) so the
    multi-second refresh is legible. Pin the wiring."""
    dash = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
    text = dash.read_text(encoding="utf-8")
    # Locate the _rematchAccept function body.
    start = text.index("async function _rematchAccept(day)")
    end = text.index("function _rematchDecline", start)
    body = text[start:end]
    # All 4 stage labels present in the body.
    for label in ("WORKOUT RESHUFFLED", "UPDATING PLAN", "UPDATING CALENDAR", "DONE"):
        assert label in body, f"missing stage label: {label}"
    # paintProgress helper drives the staged display.
    assert "paintProgress" in body
    # Final 'Plan updated' confirmation still surfaces download buttons.
    assert "Plan updated" in body
    assert "downloadZwoFile" in body
    assert "downloadFIT" in body
