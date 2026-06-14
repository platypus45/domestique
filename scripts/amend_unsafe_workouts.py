#!/usr/bin/env python3.12
"""
amend_unsafe_workouts.py — amend physiologically dangerous / corrupt ZWO workouts IN PLACE.

Driven by the injury audit (/tmp/library_injury_audit.md). The audit found a small,
identifiable dangerous tail: corrupt supramaximal power (e.g. 600% FTP), warmups/cooldowns
that ramp into/finish at supramaximal, sustained supramaximal blocks, inadequate recovery,
absurd rep counts, and a few padded HIT sessions. The owner wants them AMENDED IN PLACE into
functional, SAFE workouts that preserve each workout's training INTENT (class + rough
structure) — NOT deleted.

A file is SELECTED for amendment if it trips any genuine danger (see is_dangerous()). Once
selected, ALL six safety rules below are applied so the result is comfortably safe. Files that
are already safe are left byte-for-byte untouched. Re-running is a no-op (idempotent): an
amended file no longer trips any danger and is not re-selected.

Safety rules (Power attrs = fractions of FTP):
  1. Warmup  : rising ramp ~0.50 -> ~0.75. Never start >0.60, never ramp into the work zone.
               Keep an existing warmup that already rises and ends <=0.80 (start <=0.60).
  2. Cooldown: falling ramp ~0.65 -> ~0.40. Never end >0.60, never ramp up.
               Keep an existing cooldown that already falls and ends <=0.60.
  3. Work ceiling by class: neuromuscular micro(<=15s) 2.00 / else 1.50 · sprint 1.80 ·
               anaerobic 1.50 · vo2_short|vo2max|vo2_ladder 1.20 · threshold(_ladder) 1.05 ·
               over_under over-leg 1.10 · sweet_spot(_ladder) 0.97. Corrupt values clamp down.
  4. Recovery: for any work block >=1.20, the following recovery must be <=0.60 power AND
               >=0.5x the work duration (>=1.0x if work >=1.50). Fix 0s-rest and rest<work.
  5. Rep cap : <=30 hard reps (40 for <=15s micro-intervals). Drop extra reps; keep per-rep dose.
  6. HIT total: vo2/vo2_short/vo2_ladder <=75min, anaerobic/neuromuscular <=60min. Trim
               warmup/cooldown padding and excess reps, not the core stimulus.

Usage:
  python3.12 scripts/amend_unsafe_workouts.py            # amend in place
  python3.12 scripts/amend_unsafe_workouts.py --dry-run  # report only, write nothing
  python3.12 scripts/amend_unsafe_workouts.py --verbose  # per-file before/after summary
"""
from __future__ import annotations
import argparse
import glob
import json
import math
import os
import sys
import xml.etree.ElementTree as ET

WDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workouts")
CLASS_PATH = os.path.join(WDIR, ".content_classification.json")

# ---- class -> work-power ceiling (rule 3) ----
_CEIL = {
    "sprint": 1.80,
    "anaerobic": 1.50,
    "vo2_short": 1.20,
    "vo2max": 1.20,
    "vo2_ladder": 1.20,
    "threshold": 1.05,
    "threshold_ladder": 1.05,
    "over_under": 1.10,      # over-leg ceiling
    "sweet_spot": 0.97,
    "sweet_spot_ladder": 0.97,
}
# Classes with no intrinsic supra work (endurance/recovery/tempo/ftp_test/...) get a generous
# cap so we only ever clamp genuinely corrupt spikes, never normal authored power.
_DEFAULT_CEIL = 1.20

# Ramp tests / FTP tests are legitimate MAXIMAL protocols (a rising ramp to exhaustion, a
# 20-min all-out). They look "dangerous" to a naive sweep but are the intended workout and
# must never be amended (owner constraint). Selected by class OR filename token.
_RAMP_EXEMPT = {"ftp_test", "ramp"}


def pd_ceiling(dur_s):
    """Physiological power-duration ceiling (fraction of FTP). A work block whose peak
    exceeds this for its OWN duration is impossible (corrupt data), not real training:
    240%/15s is a legit neuromuscular sprint, 220%/3min is impossible. Duration-aware so
    genuine short sprints are NOT flagged while 600% ramps, 350%/30s and 220%/3min are."""
    if dur_s <= 15:
        return 3.0
    if dur_s <= 30:
        return 2.5
    if dur_s <= 60:
        return 2.0
    if dur_s <= 180:
        return 1.6
    return 1.4

# recovery / warmup / cooldown anchors
WU_LO, WU_HI = 0.50, 0.75          # rule 1 target ramp
CD_HI, CD_LO = 0.65, 0.40          # rule 2 target ramp
REC_CEIL = 0.60                    # rule 4 recovery power ceiling
WORK_REC_THRESH = 1.20             # rule 4 trigger
HARD_REC_THRESH = 1.50             # rule 4: needs >=1.0x rest above this

# HIT total-duration ceilings (rule 6), seconds
HIT_75 = {"vo2max", "vo2_short", "vo2_ladder"}
HIT_60 = {"anaerobic", "neuromuscular"}


def load_classes():
    with open(CLASS_PATH) as f:
        return json.load(f)["classifications"]


def class_of(classifications, fn):
    c = classifications.get(fn)
    return (c.get("primary") if c else None) or "?"


def ceiling_for(klass, dur_s):
    if klass == "neuromuscular":
        return 2.00 if dur_s <= 15 else 1.50
    return _CEIL.get(klass, _DEFAULT_CEIL)


# --------------------------------------------------------------------------------------
# A workout is represented as an ordered list of "blocks". Each block is a dict mirroring
# one ZWO element. We classify each block as warmup / cooldown / work / recovery / other by
# position + power so the rules can be applied structurally.
# --------------------------------------------------------------------------------------

def fmt_num(x):
    """Format a power/number the way the library does: trim to a compact decimal."""
    if x == int(x):
        # keep one decimal for powers like 1.0 / 0.5 to match library style for fractions,
        # but integers for Duration/Repeat/pace. Caller decides via int flag.
        return str(int(x))
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s


def parse_blocks(path):
    """Return (tree, root, workout_el, blocks). blocks are live references to ET elements."""
    tree = ET.parse(path)
    root = tree.getroot()
    w = root.find("workout")
    blocks = list(w) if w is not None else []
    return tree, root, w, blocks


def block_peak(el):
    t = el.tag
    if t == "IntervalsT":
        return float(el.get("OnPower", 0))
    if t in ("Warmup", "Cooldown", "Ramp"):
        return max(float(el.get("PowerLow", 0)), float(el.get("PowerHigh", 0)))
    if t == "SteadyState":
        return float(el.get("Power", el.get("PowerLow", 0)))
    return 0.0


def block_dur(el):
    if el.tag == "IntervalsT":
        rep = int(float(el.get("Repeat", 1)))
        return rep * (float(el.get("OnDuration", 0)) + float(el.get("OffDuration", 0)))
    return float(el.get("Duration", 0))


def expand_segs(blocks):
    """Flatten blocks into (kind, dur, peak) segments matching the audit's model."""
    segs = []
    for el in blocks:
        t = el.tag
        if t == "SteadyState":
            d = float(el.get("Duration", 0)); p = float(el.get("Power", el.get("PowerLow", 0)))
            segs.append(("SteadyState", d, p))
        elif t in ("Warmup", "Cooldown", "Ramp"):
            d = float(el.get("Duration", 0))
            p = max(float(el.get("PowerLow", 0)), float(el.get("PowerHigh", 0)))
            segs.append((t, d, p))
        elif t == "IntervalsT":
            rep = int(float(el.get("Repeat", 1)))
            ond = float(el.get("OnDuration", 0)); offd = float(el.get("OffDuration", 0))
            onp = float(el.get("OnPower", 0)); offp = float(el.get("OffPower", 0))
            for _ in range(rep):
                segs.append(("INT_ON", ond, onp))
                segs.append(("INT_OFF", offd, offp))
        elif t == "FreeRide":
            segs.append(("FreeRide", float(el.get("Duration", 0)), 0.0))
        else:
            segs.append((t, float(el.get("Duration", 0)), 0.0))
    return segs


# --------------------------- danger detection (selection) -----------------------------

def _wall_segments(blocks):
    """Expand blocks into (is_hard, is_recovery) flags for the wall check. A segment is HARD if
    its sustained level is >=1.20 with no relief; it is RECOVERY if it ends <=0.75. A descending
    ramp (ends <=0.75) is RECOVERY even if it starts high — that down-leg clears lactate."""
    out = []
    for el in blocks:
        t = el.tag
        if t in ("Warmup", "Cooldown"):
            out.append(("break", 0.0)); continue
        if t == "Ramp":
            lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", 0))
            end = hi  # ramp goes lo->hi
            # recovery if it ends easy (<=0.75); otherwise its sustained/peak level is the high end
            if end <= 0.75:
                out.append(("rec", end))
            else:
                out.append(("lvl", max(lo, hi)))
        elif t == "SteadyState":
            p = float(el.get("Power", 0))
            out.append(("rec", p) if p < 1.05 else ("lvl", p))
        elif t == "IntervalsT":
            rep = int(float(el.get("Repeat", 1)))
            onp = float(el.get("OnPower", 0)); offp = float(el.get("OffPower", 0))
            for _ in range(rep):
                out.append(("lvl", onp) if onp >= 1.05 else ("rec", onp))
                out.append(("rec", offp) if offp < 1.05 else ("lvl", offp))
        elif t == "FreeRide":
            out.append(("rec", 0.0))
        else:
            out.append(("rec", 0.0))
    return out


def _wall_violation_blocks(blocks):
    """Genuine no-recovery wall: >=4 consecutive HARD (>=1.05) levels with no easy clearance,
    where at least one adjacent pair is HIGH (both >=1.20). A recovery segment (ends <=0.75, incl.
    a descending ramp) breaks the run. Flags 0s-rest supra sprint sets; not ramp tests /
    over-unders (down-legs return to easy) nor benign sub-threshold step runs."""
    run = []
    def flush(r):
        if len(r) >= 4:
            for i in range(len(r) - 1):
                if r[i] >= 1.20 and r[i + 1] >= 1.20:
                    return True
        return False
    for kind, lvl in _wall_segments(blocks):
        if kind == "lvl":
            run.append(lvl)
        else:  # break or rec
            if flush(run):
                return True
            run = []
    return flush(run)


def is_dangerous(blocks, klass, fn=""):
    """True if the workout trips a genuine INJURY hazard. Reasons returned for reporting.

    Selection = real hazards ONLY: physiologically-impossible (corrupt) power, sustained
    supramaximal, inadequate recovery, and no-recovery walls. Ramp / FTP tests are EXEMPT
    (legitimate maximal protocols — owner constraint). Mild quality issues (a hard
    warmup/cooldown ramp, a high-but-recovered rep count) are NOT selectors — they would
    drag in ~250 legit sprint/interval files; once a file IS selected for a real hazard the
    amend rules still clean its warmup/cooldown/reps.
    """
    if not blocks:
        return []
    # Ramp / FTP test EXEMPT — legitimate maximal protocol, never a hazard.
    if klass in _RAMP_EXEMPT or "ramp" in fn or "ftp_test" in fn or "_ftp_" in fn:
        return []
    segs = expand_segs(blocks)
    reasons = []

    # corrupt / physiologically-impossible power: a work block whose peak exceeds the
    # power-duration ceiling for ITS OWN duration. 240%/15s is a legit neuromuscular sprint;
    # 220%/3min and the 600% ramps are impossible (corrupt data). Duration-aware, so real
    # short sprints are NOT flagged — the flat 1.80 sweep wrongly caught ~120 of them.
    for kind, d, pk in segs:
        if kind in ("Warmup", "Cooldown"):
            continue
        if pk > pd_ceiling(d) + 1e-9:
            reasons.append("corrupt_power")
            break

    # sustained supramaximal: >1.50 held for >60s with no relief — true hazard.
    for kind, d, pk in segs:
        if kind not in ("Warmup", "Cooldown") and d > 60 and pk > 1.50:
            reasons.append("sustained_supra")
            break

    # inadequate recovery: >=3 work@>=1.30(>=30s) each followed by rest<work.
    inadq = 0
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if a[0] in ("INT_ON", "SteadyState", "Ramp") and a[2] >= 1.30 and a[1] >= 30:
            if b[0] in ("INT_OFF", "SteadyState") and b[2] < 0.75 and b[1] < a[1]:
                inadq += 1
    if inadq >= 3:
        reasons.append("inadequate_rec")

    # no-recovery wall: a genuine run of back-to-back HARD efforts with no clearance (the
    # 0s-rest supra sets, incl. mislabeled "endurance" files hiding a climbing wall).
    # Descending ramps / easy blocks count as recovery, so ramp tests and over-unders
    # (which return to easy each cycle) are NOT flagged.
    if _wall_violation_blocks(blocks):
        reasons.append("no_recovery_wall")

    return sorted(set(reasons))


# ------------------------------- amendment (rewrite) ----------------------------------

def amend_warmup(blocks):
    """Rule 1. Returns True if changed. Operates on blocks[0] if it's the opening warmup."""
    if not blocks:
        return False
    el = blocks[0]
    t = el.tag
    if t == "Warmup":
        lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", lo))
        # keep if it already rises sanely (start<=0.60, end<=0.80)
        if lo <= 0.60 and hi <= 0.80 and hi >= lo:
            return False
        el.set("PowerLow", "0.5"); el.set("PowerHigh", "0.75")
        return True
    if t == "Ramp":
        lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", lo))
        # leading Ramp acting as warmup
        if hi > 0.80 or lo > 0.60 or hi < lo:
            el.set("PowerLow", "0.5"); el.set("PowerHigh", "0.75")
            return True
        return False
    if t == "SteadyState":
        p = float(el.get("Power", 0))
        d = float(el.get("Duration", 0))
        # an opening steady block that sits in/above the work zone is not a warmup; convert the
        # leading easy-intent block to a real rising warmup only if it's clearly the opener and
        # supramaximal/hard. (>=60s and >0.80)
        if d >= 60 and p > 0.80:
            new = ET.Element("Warmup")
            new.set("Duration", el.get("Duration"))
            new.set("PowerLow", "0.5"); new.set("PowerHigh", "0.75")
            new.set("pace", el.get("pace", "0"))
            _replace_attrs(el, new)
            return True
        return False
    if t == "FreeRide":
        return False  # free ride opener is benign (no target)
    return False


def amend_cooldown(blocks):
    """Rule 2. Operates on blocks[-1] if it's a closing cooldown-ish block."""
    if not blocks:
        return False
    el = blocks[-1]
    t = el.tag
    if t == "Cooldown":
        lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", lo))
        # ZWO Cooldown: PowerLow = start? In library, Cooldown PowerLow->PowerHigh is start->end.
        start, end = lo, hi
        # keep if it falls and ends <=0.60
        if end <= 0.60 and end <= start:
            return False
        el.set("PowerLow", "0.65"); el.set("PowerHigh", "0.4")
        return True
    if t == "Ramp":
        lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", lo))
        if hi > 0.60 or hi > lo:  # ends hard or rises
            el.set("PowerLow", "0.65"); el.set("PowerHigh", "0.4")
            return True
        return False
    if t == "SteadyState":
        p = float(el.get("Power", 0)); d = float(el.get("Duration", 0))
        if d >= 60 and p > 0.60:
            # turn a hard final steady block into a proper falling cooldown
            new = ET.Element("Cooldown")
            new.set("Duration", el.get("Duration"))
            new.set("PowerLow", "0.65"); new.set("PowerHigh", "0.4")
            new.set("pace", el.get("pace", "0"))
            _replace_attrs(el, new)
            return True
        return False
    return False


def _replace_attrs(el, new):
    """Replace el's tag+attrs with new's (in place, preserving identity in the tree)."""
    el.tag = new.tag
    el.attrib.clear()
    for k, v in new.attrib.items():
        el.set(k, v)


def amend_swap_corrupt_intervals(blocks):
    """Pre-pass for corrupt IntervalsT where on/off are swapped (OffPower > OnPower and the off
    is supramaximal, e.g. On 0.83 / Off 2.00). Swap so the hard effort is the ON (work) interval
    and the easy one is the OFF (recovery); the subsequent ceiling/recovery rules then make it a
    proper interval set, preserving the workout's hard-effort intent."""
    changed = False
    for el in blocks:
        if el.tag != "IntervalsT":
            continue
        onp = float(el.get("OnPower", 0)); offp = float(el.get("OffPower", 0))
        if offp > onp and offp > 1.50:
            ond = el.get("OnDuration"); offd = el.get("OffDuration")
            el.set("OnPower", fmt_pow(offp)); el.set("OffPower", fmt_pow(onp))
            el.set("OnDuration", offd); el.set("OffDuration", ond)
            changed = True
    return changed


def amend_work_ceiling(blocks, klass):
    """Rule 3. Clamp every work block's peak to its class+duration ceiling."""
    changed = False
    n = len(blocks)
    for i, el in enumerate(blocks):
        t = el.tag
        is_first = i == 0
        is_last = i == n - 1
        if t == "Warmup" or t == "Cooldown":
            continue
        if t == "IntervalsT":
            dur = float(el.get("OnDuration", 0))
            ceil = ceiling_for(klass, dur)
            onp = float(el.get("OnPower", 0))
            if onp > ceil + 1e-9:
                el.set("OnPower", fmt_pow(ceil)); changed = True
            # the OFF (recovery) interval must never exceed the work ceiling either — a few
            # corrupt files have on/off swapped (e.g. OnPower 0.83 / OffPower 2.00). Clamp the
            # off interval down to a true recovery (rule 4 caps it further if On is hard).
            offp = float(el.get("OffPower", 0))
            if offp > ceil + 1e-9:
                el.set("OffPower", fmt_pow(min(ceil, REC_CEIL))); changed = True
        elif t == "SteadyState":
            # skip if this steady block is the opening warmup / closing cooldown (handled elsewhere)
            d = float(el.get("Duration", 0)); p = float(el.get("Power", 0))
            ceil = ceiling_for(klass, d)
            if p > ceil + 1e-9:
                el.set("Power", fmt_pow(ceil)); changed = True
        elif t == "Ramp":
            # interior ramp (over/under leg). Clamp the high end to ceiling; keep direction.
            if is_first or is_last:
                continue
            lo = float(el.get("PowerLow", 0)); hi = float(el.get("PowerHigh", 0))
            ceil = ceiling_for(klass, float(el.get("Duration", 0)))
            if hi > ceil + 1e-9:
                el.set("PowerHigh", fmt_pow(ceil)); changed = True
            if lo > ceil + 1e-9:
                el.set("PowerLow", fmt_pow(ceil)); changed = True
    return changed


def fmt_pow(x):
    """Format a power fraction compactly (e.g. 1.5, 0.97, 1.2)."""
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    if s == "" or s == "-0":
        s = "0"
    return s


def amend_recovery(blocks):
    """Rule 4. For work blocks >=1.20, ensure following recovery <=0.60 and adequate length.

    Handles two shapes:
      * IntervalsT: fix OffPower (<=0.60) and OffDuration (>=0.5x On, >=1.0x if On>=1.50).
      * SteadyState work followed by SteadyState recovery: fix the recovery block.
    """
    changed = False
    # IntervalsT: self-contained on/off
    for el in blocks:
        if el.tag == "IntervalsT":
            onp = float(el.get("OnPower", 0))
            if onp >= WORK_REC_THRESH:
                ond = float(el.get("OnDuration", 0))
                offp = float(el.get("OffPower", 0))
                offd = float(el.get("OffDuration", 0))
                need = ond * (1.0 if onp >= HARD_REC_THRESH else 0.5)
                if offp > REC_CEIL:
                    el.set("OffPower", fmt_pow(REC_CEIL)); changed = True
                if offd < need - 1e-6:
                    el.set("OffDuration", str(int(math.ceil(need)))); changed = True
    # SteadyState/Ramp work -> recovery-slot pairs. The block following a >=1.20 work effort is
    # its recovery slot when it is clearly easier than the work (peak < the work peak AND below
    # the 1.20 work-trigger). Such a slot must be <=0.60 and long enough. This also pulls down
    # over-FTP "active recovery" floats (e.g. 1.05/0.80 between micro-reps) that otherwise leave
    # the rider with no real lactate clearance. A following block at >=1.20 is another work rep,
    # not a recovery slot, and is left to the rep-structure rules.
    n = len(blocks)
    for i in range(n - 1):
        a = blocks[i]
        ap = block_peak(a)
        if a.tag not in ("SteadyState", "Ramp") or ap < WORK_REC_THRESH:
            continue
        ad = float(a.get("Duration", 0))
        need = ad * (1.0 if ap >= HARD_REC_THRESH else 0.5)
        b = blocks[i + 1]
        if b.tag == "Cooldown":
            continue  # falling cooldown after final work is fine
        bp = block_peak(b)
        if b.tag in ("SteadyState", "Ramp") and bp < min(ap, WORK_REC_THRESH):
            # this is the recovery slot
            bd = float(b.get("Duration", 0))
            if b.tag == "Ramp":
                # collapse a recovery ramp to a flat easy SteadyState recovery
                if bp > REC_CEIL or float(b.get("PowerLow", 0)) > REC_CEIL or float(b.get("PowerHigh", 0)) > REC_CEIL:
                    new = ET.Element("SteadyState")
                    new.set("Duration", str(int(max(bd, need))))
                    new.set("Power", "0.55")
                    new.set("pace", b.get("pace", "0"))
                    _replace_attrs(b, new)
                    changed = True
            else:
                if bp > REC_CEIL:
                    b.set("Power", "0.55"); changed = True
                if bd < need - 1e-6:
                    b.set("Duration", str(int(math.ceil(need)))); changed = True
    return changed


def amend_insert_recovery(blocks):
    """Break up runs of >=3 back-to-back hard SteadyState work blocks (no recovery between) by
    inserting a real recovery block after each work block. This repairs corrupt 'walls' such as
    the 600%-ramp files which, once power-clamped, collapse to many identical supra blocks with
    zero rest. IntervalsT sets carry their own off-interval and are skipped here. The subsequent
    rep-cap and HIT-duration passes then trim the (now well-formed) set down to a sane dose.
    """
    # find maximal runs of consecutive SteadyState blocks with peak >= 1.05 (no easy block between)
    runs = []
    i = 0
    n = len(blocks)
    while i < n:
        if blocks[i].tag == "SteadyState" and float(blocks[i].get("Power", 0)) >= 1.05:
            j = i
            while j < n and blocks[j].tag == "SteadyState" and float(blocks[j].get("Power", 0)) >= 1.05:
                j += 1
            if j - i >= 3:
                runs.append((i, j))  # [i, j)
            i = j
        else:
            i += 1
    if not runs:
        return False
    # rebuild block list inserting recovery after each work block in a run (except the last in run)
    out = []
    changed = False
    run_map = {start: end for start, end in runs}
    k = 0
    while k < n:
        if k in run_map:
            end = run_map[k]
            for idx in range(k, end):
                wb = blocks[idx]
                out.append(wb)
                wp = float(wb.get("Power", 0))
                wd = float(wb.get("Duration", 0))
                # recovery length: >=1.0x work if work>=1.50 else >=0.5x work (rule 4)
                rec_d = int(math.ceil(wd * (1.0 if wp >= HARD_REC_THRESH else 0.5)))
                if idx < end - 1:  # insert recovery between consecutive work reps, not after last
                    rec = ET.Element("SteadyState")
                    rec.set("Duration", str(max(rec_d, 30)))
                    rec.set("Power", "0.5")
                    rec.set("pace", wb.get("pace", "0"))
                    out.append(rec)
            changed = True
            k = end
        else:
            out.append(blocks[k])
            k += 1
    if changed:
        blocks[:] = out
    return changed


def _hard_rep_indices(blocks):
    """Indices of blocks that are 'hard reps' for the cap, with their effective rep count.

    Returns list of (block_index, rep_count, per_rep_dur). IntervalsT counts as Repeat reps.
    SteadyState >=1.05 and <=300s counts as 1 rep.
    """
    out = []
    for i, el in enumerate(blocks):
        if el.tag == "IntervalsT":
            onp = float(el.get("OnPower", 0))
            ond = float(el.get("OnDuration", 0))
            if onp >= 1.05 and ond <= 300:
                out.append((i, int(float(el.get("Repeat", 1))), ond))
        elif el.tag == "SteadyState":
            p = float(el.get("Power", 0)); d = float(el.get("Duration", 0))
            if p >= 1.05 and d <= 300:
                out.append((i, 1, d))
    return out


def amend_rep_cap(blocks):
    """Rule 5. Cap hard reps at 30 (40 for <=15s micro). Trim from the END, keeping dose."""
    reps = _hard_rep_indices(blocks)
    total = sum(r for _, r, _ in reps)
    if total == 0:
        return False
    micro = all(pr <= 15 for _, _, pr in reps)
    cap = 40 if micro else 30
    if total <= cap:
        return False
    # reduce from the last rep-bearing blocks first
    to_drop = total - cap
    changed = False
    drop_block_idxs = []
    for bi, rc, pr in reversed(reps):
        if to_drop <= 0:
            break
        el = blocks[bi]
        if el.tag == "IntervalsT":
            if rc <= to_drop:
                drop_block_idxs.append(bi)
                to_drop -= rc
            else:
                el.set("Repeat", str(rc - to_drop))
                to_drop = 0
            changed = True
        else:  # SteadyState single rep — drop the work block AND a following recovery if present
            drop_block_idxs.append(bi)
            # also drop the immediately following recovery steady block to keep structure clean
            if bi + 1 < len(blocks) and blocks[bi + 1].tag == "SteadyState" and float(blocks[bi + 1].get("Power", 1)) < 0.75:
                drop_block_idxs.append(bi + 1)
            to_drop -= 1
            changed = True
    # remove dropped blocks (highest index first)
    for bi in sorted(set(drop_block_idxs), reverse=True):
        del blocks[bi]
    return changed


def total_duration(blocks):
    return sum(block_dur(el) for el in blocks)


def amend_hit_duration(blocks, klass):
    """Rule 6. Trim total session to the class HIT ceiling.

    Order of trimming: (a) cap excessively long warmup/cooldown padding to 15min each,
    (b) trim long pure-easy filler steadies, (c) drop trailing rep sets / reduce Repeat —
    but never below a sane core (>= the larger of cap-respecting minimum and 4 reps / 1 set).
    """
    ceil_s = None
    if klass in HIT_75:
        ceil_s = 75 * 60
    elif klass in HIT_60:
        ceil_s = 60 * 60
    if ceil_s is None:
        return False
    if total_duration(blocks) <= ceil_s:
        return False
    changed = False

    # (a) cap warmup / cooldown to 15 min
    for el in (blocks[0] if blocks else None, blocks[-1] if blocks else None):
        if el is None:
            continue
        if el.tag in ("Warmup", "Cooldown") and float(el.get("Duration", 0)) > 900:
            el.set("Duration", "900"); changed = True
    if total_duration(blocks) <= ceil_s:
        return changed

    # (b) trim long easy filler SteadyState (<0.75 power, interior), each down to <=300s
    for i, el in enumerate(blocks):
        if total_duration(blocks) <= ceil_s:
            break
        if el.tag == "SteadyState" and float(el.get("Power", 1)) < 0.75 and float(el.get("Duration", 0)) > 300:
            el.set("Duration", "300"); changed = True
    if total_duration(blocks) <= ceil_s:
        return changed

    # (c) drop trailing work efforts (reduce Repeat / drop whole work blocks, each with its
    # following recovery) until under ceiling. Considers ALL work blocks incl. long intervals
    # (>300s), not just short reps. Protect the core: never reduce below 2 work efforts total.
    guard = 0
    while total_duration(blocks) > ceil_s and guard < 200:
        guard += 1
        work = _all_work_indices(blocks)
        total_efforts = sum(r for _, r, _, _ in work)
        if total_efforts <= 2:
            break
        bi, rc, _pr, tag = work[-1]  # last work effort
        el = blocks[bi]
        if tag == "IntervalsT" and rc > 1:
            el.set("Repeat", str(rc - 1)); changed = True
        else:
            _drop_with_filler(blocks, bi); changed = True
    return changed


def _all_work_indices(blocks):
    """All work efforts for HIT-duration trimming: (index, rep_count, per_rep_dur, tag).
    Work = peak >= 1.05, ANY duration. IntervalsT counts Repeat efforts; others count 1."""
    out = []
    for i, el in enumerate(blocks):
        if el.tag == "IntervalsT" and float(el.get("OnPower", 0)) >= 1.05:
            out.append((i, int(float(el.get("Repeat", 1))), float(el.get("OnDuration", 0)), "IntervalsT"))
        elif el.tag == "SteadyState" and float(el.get("Power", 0)) >= 1.05:
            out.append((i, 1, float(el.get("Duration", 0)), "SteadyState"))
    return out


def _drop_with_filler(blocks, bi):
    idxs = [bi]
    if bi + 1 < len(blocks) and blocks[bi + 1].tag in ("SteadyState", "FreeRide") and float(blocks[bi + 1].get("Power", 0) or 0) < 0.75:
        idxs.append(bi + 1)
    for i in sorted(set(idxs), reverse=True):
        del blocks[i]


# ------------------------------- serialization ----------------------------------------

# attribute output order per element tag (matches library style)
_ATTR_ORDER = {
    "Warmup": ["Duration", "PowerLow", "PowerHigh", "pace"],
    "Cooldown": ["Duration", "PowerLow", "PowerHigh", "pace"],
    "Ramp": ["Duration", "PowerLow", "PowerHigh", "pace"],
    "SteadyState": ["Duration", "Power", "pace"],
    "IntervalsT": ["Repeat", "OnDuration", "OffDuration", "OnPower", "OffPower", "pace"],
    "FreeRide": ["Duration", "FlatRoad"],
}


def serialize(root, workout_el, blocks):
    """Reconstruct the ZWO text in the library's exact style. blocks is the final element list."""
    lines = ["<?xml version='1.0' encoding='utf-8'?>", "<workout_file>"]
    # top-level children in original order, except <workout> which we rebuild from blocks
    for child in list(root):
        if child.tag == "workout":
            lines.append("    <workout>")
            for el in blocks:
                lines.append("        " + _el_line(el))
            lines.append("    </workout>")
        else:
            txt = (child.text or "").strip()
            lines.append(f"    <{child.tag}>{txt}</{child.tag}>")
    lines.append("</workout_file>")
    return "\n".join(lines)


def _el_line(el):
    order = _ATTR_ORDER.get(el.tag)
    attrs = dict(el.attrib)
    parts = []
    if order:
        for k in order:
            if k in attrs:
                parts.append(f'{k}="{attrs.pop(k)}"')
        # any leftover attrs (unexpected) appended in sorted order
        for k in sorted(attrs):
            parts.append(f'{k}="{attrs[k]}"')
    else:
        for k in sorted(attrs):
            parts.append(f'{k}="{attrs[k]}"')
    body = " ".join(parts)
    return f"<{el.tag} {body} />" if body else f"<{el.tag} />"


# --------------------------------- driver ---------------------------------------------

def amend_file(path, classifications, dry_run=False):
    """Return (changed: bool, reasons: list, before: dict, after: dict)."""
    fn = os.path.basename(path)
    klass = class_of(classifications, fn)
    tree, root, w, blocks = parse_blocks(path)
    if w is None or not blocks:
        return False, [], {}, {}

    reasons = is_dangerous(blocks, klass, fn)
    if not reasons:
        return False, [], {}, {}

    before = _summary(blocks, klass)

    # blocks is a live list of ET elements; we mutate elements in place and del from list.
    # Some amend_* functions need the list to shrink (rep cap, hit duration).
    amend_warmup(blocks)
    amend_cooldown(blocks)
    amend_swap_corrupt_intervals(blocks)
    amend_work_ceiling(blocks, klass)
    amend_insert_recovery(blocks)
    amend_recovery(blocks)
    amend_rep_cap(blocks)
    amend_hit_duration(blocks, klass)
    # re-run ceiling+recovery after structural trims (defensive; idempotent)
    amend_work_ceiling(blocks, klass)
    amend_recovery(blocks)

    after = _summary(blocks, klass)
    text = serialize(root, w, blocks)
    if not text.endswith("\n"):
        pass  # library files have no trailing newline

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return True, reasons, before, after


def _summary(blocks, klass):
    segs = expand_segs(blocks)
    work = [s for s in segs if s[0] not in ("Warmup", "Cooldown") and s[2] > 0]
    peak = max((s[2] for s in work), default=0.0)
    reps = sum(1 for s in segs if s[2] >= 1.05 and s[1] <= 300 and s[0] not in ("Warmup", "Cooldown"))
    wu = blocks[0] if blocks else None
    cd = blocks[-1] if blocks else None
    wu_s = f"{wu.tag}:{wu.get('PowerLow', wu.get('Power','?'))}->{wu.get('PowerHigh', wu.get('Power','?'))}" if wu is not None else "?"
    cd_s = f"{cd.tag}:{cd.get('PowerLow', cd.get('Power','?'))}->{cd.get('PowerHigh', cd.get('Power','?'))}" if cd is not None else "?"
    return {
        "peak": round(peak, 2),
        "reps": reps,
        "dur_min": round(total_duration(blocks) / 60, 1),
        "warmup": wu_s,
        "cooldown": cd_s,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Amend dangerous ZWO workouts in place.")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--verbose", action="store_true", help="print per-file before/after")
    ap.add_argument("--workout-dir", default=WDIR)
    args = ap.parse_args(argv)

    classifications = load_classes()
    files = sorted(glob.glob(os.path.join(args.workout_dir, "*.zwo")))

    amended = []
    from collections import Counter
    reason_counts = Counter()
    class_counts = Counter()
    for path in files:
        changed, reasons, before, after = amend_file(path, classifications, dry_run=args.dry_run)
        if changed:
            fn = os.path.basename(path)
            amended.append((fn, reasons, before, after))
            for r in set(reasons):
                reason_counts[r] += 1
            class_counts[class_of(classifications, fn)] += 1
            if args.verbose:
                print(f"{fn} {reasons}")
                print(f"    before: {before}")
                print(f"    after:  {after}")

    print(f"\n{'DRY-RUN: would amend' if args.dry_run else 'Amended'} {len(amended)} files")
    print("By reason:", dict(reason_counts.most_common()))
    print("By class :", dict(class_counts.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
