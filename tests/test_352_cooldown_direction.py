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
    # v3.7.0 re-pointed these values, not the test's intent. 0.75 FTP is
    # 186 W at this rider's FTP and sits at or above the first lactate
    # threshold for many riders — a tempo effort wearing a cooldown's tag.
    # 0.60 sits just under the clearance optimum (~0.64 FTP), deliberately:
    # individual LT1 scatters, and the cheaper error is slightly too easy.
    assert float(last.get("PowerLow")) == 0.60
    assert float(last.get("PowerHigh")) == 0.25
    assert "Cooldown: 8min from 60% to 25% FTP" in (root.findtext("description") or "")


# ── v3.7.0: a cooldown is easy, and never harder than what preceded it ───────
# The rider's report: after 6x2min VO2max the session finished at 28 % FTP and
# the "cooldown" ramp began at 75 % — 186 W, a step UP of 117 W onto a rider
# who was already done. 1647 of 3672 cooldowns did this. These four assertions
# are the invariant; scripts/fix_cooldowns_v37.py is what established it.

import re
import sys
sys.path.insert(0, str(WORKOUTS.parent / "scripts"))
from fix_cooldowns_v37 import (  # noqa: E402
    CD_START_MAX, CD_END, prev_end_power, _CD_CLAUSE)


def _cooldown_files():
    for p in sorted(WORKOUTS.glob("*.zwo")):
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError:
            continue
        w = root.find("workout")
        if w is None:
            continue
        els = list(w)
        cds = [i for i, e in enumerate(els) if e.tag == "Cooldown"]
        if not cds:
            continue
        yield p, root, els, cds


def test_no_cooldown_steps_up_from_the_segment_before_it():
    """The rider's actual complaint. A segment that RAISES your power is not a
    cooldown, whatever the tag says — this is the one rule here with no
    research caveat attached."""
    bad = []
    for p, _root, els, cds in _cooldown_files():
        i = cds[-1]
        lo = float(els[i].get("PowerLow"))
        prev = prev_end_power(els, i)
        if prev is not None and lo > prev + 1e-9:
            bad.append(f"{p.name}: {prev:.2f} -> {lo:.2f}")
    assert bad == [], f"{len(bad)} cooldowns step UP (was 1647): {bad[:8]}"


def test_cooldowns_are_actually_easy():
    """Start at or below the top of the clearance-optimal band and end at or
    below 0.45. Above the first lactate threshold the muscle is still net
    PRODUCING lactate, so a hot cooldown is worse at the one job a cooldown
    reliably does (Devlin 2014 PMID 24739289, Menzies 2010 PMID 20544484)."""
    hot = []
    for p, _root, els, cds in _cooldown_files():
        i = cds[-1]
        lo, hi = float(els[i].get("PowerLow")), float(els[i].get("PowerHigh"))
        if lo > CD_START_MAX + 1e-9 or hi > CD_END + 1e-9:
            hot.append(f"{p.name}: {lo:.2f}->{hi:.2f}")
    assert hot == [], f"{len(hot)} cooldowns too hard (was 1296): {hot[:8]}"


def test_exactly_one_cooldown_and_it_is_last():
    """What makes "the segment before the cooldown" well defined."""
    bad = []
    for p, _root, els, cds in _cooldown_files():
        if len(cds) > 1 or cds[-1] != len(els) - 1:
            bad.append(p.name)
    assert bad == [], f"cooldown not the single final segment: {bad[:8]}"


def test_the_description_still_tells_the_truth():
    """The prose is shown in trainer apps and pushed to intervals.icu. A power
    edit without a prose edit ships a user-visible lie."""
    bad = []
    for p, root, els, cds in _cooldown_files():
        text = root.findtext("description") or ""
        m = _CD_CLAUSE.search(text)
        if not m:
            continue
        lo, hi = float(els[cds[-1]].get("PowerLow")), float(els[cds[-1]].get("PowerHigh"))
        if m.group("flat"):
            ok = abs(lo - hi) < 0.02 and round(hi * 100) == int(m.group("flat"))
        else:
            ok = (round(lo * 100) == int(m.group("a"))
                  and round(hi * 100) == int(m.group("b")))
        if not ok:
            bad.append(f"{p.name}: attrs {lo:.2f}->{hi:.2f} vs {m.group(0)!r}")
    assert bad == [], f"{len(bad)} descriptions disagree with the file: {bad[:5]}"


def test_the_classifier_cannot_see_the_cooldown():
    """The coupling that made this fix dangerous, pinned.

    Whole-ride zone accounting counted cooldown seconds, so easing every
    cooldown moved time from Z2 to Z1 and would have relabelled 41 workouts —
    real sessions becoming ``recovery`` and routing onto recovery days — with
    no change to their actual stimulus. Rewriting a file's cooldown to ANY
    legal value must not change what the workout is.
    """
    import importlib
    import xml.etree.ElementTree as _ET
    sys.path.insert(0, str(WORKOUTS.parent / "scripts"))
    clc = importlib.import_module("classify_library_content")

    sample = sorted(WORKOUTS.glob("*.zwo"))[::250]      # ~18 files, all shapes
    assert len(sample) >= 10, "sample too small to be meaningful"
    moved = []
    for p in sample:
        before = clc.classify_zwo_v104(p).get("primary")
        text = p.read_text(encoding="utf-8")
        if "<Cooldown" not in text:
            continue
        for lo, hi in ((0.60, 0.45), (0.45, 0.30), (0.30, 0.30)):
            alt = re.sub(
                r'(<Cooldown\b[^>]*?PowerLow=")[^"]*("[^>]*?PowerHigh=")[^"]*(")',
                lambda m: f'{m.group(1)}{lo:.2f}{m.group(2)}{hi:.2f}{m.group(3)}',
                text)
            tmp = p.parent / f".__cdprobe_{p.name}"
            try:
                tmp.write_text(alt, encoding="utf-8")
                after = clc.classify_zwo_v104(tmp).get("primary")
            finally:
                tmp.unlink(missing_ok=True)
            if after != before:
                moved.append(f"{p.name}: {before} -> {after} at {lo:.2f}/{hi:.2f}")
    assert moved == [], (
        "cooldown power still decides what a workout is:\n" + "\n".join(moved))
