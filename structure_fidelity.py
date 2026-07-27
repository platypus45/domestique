"""Structure fidelity — "did you ride the SHAPE that was prescribed?"

Advisory axis for post-ride evaluation (companion to execution_score, which
grades duration/TSS/time-in-zone only and cannot see structural failures:
10 of 13 prescribed reps, a smeared ERG square wave, or a skipped final
block all survive TiZ + TSS nearly intact). This module compares the
PRESCRIBED .zwo segment timeline against the DELIVERED 1 Hz power trace
and produces a per-rep fidelity result.

Pure + deterministic, stdlib only. The only I/O helper is
:func:`parse_zwo_file` (thin wrapper for callers); everything else takes
plain values. No numpy, no DTW — a coarse global alignment plus per-segment
window statistics, tuned so a normally-lagged ERG execution scores clean.

Prescribed side — :func:`parse_zwo_text`:
  Parses the small ZWO dialect used by the library (stdlib xml.etree) into
  an ABSOLUTE segment timeline. Tags: Warmup, SteadyState, Ramp, IntervalsT
  (expanded Repeat × on/off), Cooldown, FreeRide. Each segment is a dict
  {"kind", "start_s", "dur_s", "lo", "hi"} where lo/hi are the FTP-fraction
  targets at segment start/end (steady: lo == hi; FreeRide: both None).
  Warmup/Cooldown/Ramp all ramp lo=PowerLow → hi=PowerHigh, matching the
  library's own generated descriptions ("Cooldown: 5min from 38% to 49%").

Delivered side + matching — :func:`score_structure`:
  1. Target step function: per-second target watts from the timeline
     (linear interpolation inside ramps; FreeRide seconds undefined).
  2. Global alignment: offset o in ±ALIGN_MAX_OFFSET_S (1 s steps) that
     minimizes mean absolute error between target[t] and watts[t + o] —
     a cross-correlation-style search over the target step function vs the
     trace. Offsets whose overlap covers < max(60 s, half the defined
     target seconds) are skipped; no valid offset → o = 0. Positive o
     means the trace has o seconds of riding before the workout started.
  3. WORK segments: midpoint target (lo+hi)/2 >= WORK_FLOOR_FRAC (0.75 —
     everything from tempo up; recovery valleys, z2 and cooldowns are
     structure carriers, not graded reps). Per work segment, over the
     aligned window:
       * on_target_frac — fraction of seconds within the tolerance band
         |w − target| <= max(TOL_FRAC·target, TOL_MIN_W watts), after
         dropping the first TRANSIENT_GRACE_S seconds of the segment
         (ERG controllers need 2-3 s to close on a new step; that lag is
         normal execution, not infidelity — a 10 s+ smear still shows).
       * mean_ratio — delivered mean watts / target mean watts.
       * missing — the rep was not ridden: absent-or-below-floor seconds
         exceed MISSING_BELOW_FLOOR_FRAC of the prescribed duration,
         where the floor is min(WORK_FLOOR_FRAC, MISSING_TARGET_FRAC ×
         midpoint) × FTP (the second term keeps a tempo rep ridden 2 %
         under target from being called skipped) and seconds past the end
         of the trace count as absent (rider stopped).

Result dict (field names are the contract, pinned by tests):
  {
    "reps_prescribed":    int,          # work segments in the timeline
    "reps_delivered":     int,          # prescribed − missing
    "rep_completion":     float,        # delivered / prescribed (3 dp)
    "mean_on_target_pct": float|None,   # mean on-band % over DELIVERED reps
    "mean_power_ratio":   float|None,   # mean per-rep mean_ratio over
                                        # delivered reps (smear indicator:
                                        # lag eats the rep from the front)
    "alignment_offset_s": int,
    "worst_segment":      dict|None,    # lowest on_target_frac (missing
                                        # reps count as 0.0), same row
                                        # shape as "segments" entries
    "segments":           list[dict],   # per work segment: {"index",
                                        # "start_s", "dur_s", "target_frac",
                                        # "mean_ratio", "on_target_frac",
                                        # "missing"}
  }
:func:`score_structure` returns None when it cannot honestly grade:
no segments, no watts, unusable FTP, or a timeline with zero work
segments (pure endurance rides have no reps to count).

Block grading — :func:`score_blocks`:
  A different question, off different data: "which prescribed BLOCKS did I
  actually do?" The rider marks a lap per block and intervals.icu returns
  each lap with the offset on the ride clock where it started, so no 1 Hz
  trace and no FTP value are needed.

  Four earlier versions inferred which lap was which block from the shape of
  the lap alone, and each shipped a different class of confident wrong
  verdict — an abandoned session certified complete, a block ridden harder
  than asked reported unridden, a completed block called missing because the
  recoveries ran long. All of those readings are admissible from shape. The
  offsets are what rules them out, and they are the whole difference.

  The alignment runs over SLOTS — the blocks plus every other prescribed
  stretch at or above the work floor, since a head unit types a lead-in
  effort or a warm-up ramp step "work" too and a block will otherwise steal
  its lap. It scores block-seconds explained (above a quality floor, so a
  poor pairing is worth less than none) less a cost per step for the gap
  between one slot and the next running differently from the plan. That cost
  is measured against the pace THIS rider is keeping — fitted once from the
  session's own recoveries — so stretching every recovery costs nothing,
  while stretching one costs, which is what a block gone by looks like.

  Then four gates, any of which returns None rather than a verdict: a block
  whose reading is not decisively better than the opposite reading; a block
  called unridden with a lap that could have been it sitting spare; a lap
  read as one block while it sits on top of another; and a lap spanning a
  run of blocks, which is a lap-button press that never happened and cannot
  be recovered. A missing report is recoverable. A false green tick is not.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

__all__ = [
    "parse_zwo_text", "parse_zwo_file", "score_structure", "score_blocks",
    "WORK_FLOOR_FRAC", "TOL_FRAC", "TOL_MIN_W", "TRANSIENT_GRACE_S",
    "ALIGN_MAX_OFFSET_S", "MISSING_BELOW_FLOOR_FRAC", "MISSING_TARGET_FRAC",
    "LAP_SHORT_FRAC", "LAP_POWER_TOL_FRAC", "LAP_BAND_UNDER",
    "LAP_BAND_GRAY",
]

# ── Locked fidelity constants (documented in the module docstring) ──────────
WORK_FLOOR_FRAC = 0.75          # midpoint >= this ⇒ segment is a graded rep
TOL_FRAC = 0.05                 # on-target band: ±5 % of the second's target…
TOL_MIN_W = 10.0                # …or ±10 W, whichever is larger
TRANSIENT_GRACE_S = 3           # ERG step-response seconds excluded per rep
ALIGN_MAX_OFFSET_S = 120        # global alignment search, ±s at 1 s steps
MISSING_BELOW_FLOOR_FRAC = 0.5  # absent/below-floor > this ⇒ rep missing
MISSING_TARGET_FRAC = 0.90      # floor also capped at 90 % of the rep target

# ── Lap-based block grading (score_blocks) ──────────────────────────────────
# The rider marks a lap per block, and intervals.icu returns each lap with the
# offset on the ride clock where it STARTED (moving seconds — see
# ride_storage._normalize_icu_activity). Those offsets turn the lap list into a
# TIMELINE, and the timeline is what gets graded: each block is judged by the
# work delivered in the window where it was expected, never by which lap "won"
# it. Five generations of this feature failed by letting lap identity decide a
# block's fate; identity now only pins the clock.

LAP_SHORT_FRAC = 0.80           # window coverage < this ⇒ "partial"
LAP_COV_DONE = LAP_SHORT_FRAC   # …the same number, named for what it does now
LAP_TRIVIAL_FRAC = 0.25         # coverage below this ⇒ not ridden
LAP_TRIVIAL_LAP_S = 5.0         # below this, a lap with no power is noise
LAP_POWER_TOL_FRAC = 0.10       # |delivered − target| within this ⇒ on target

# The intensity bands, all relative to the block's own target. AT-TARGET work
# (≥ 0.90×) counts toward a block; an over/under's 90 % leg is 0.86 of its
# 105 % block and correctly does not. There is no upper bound for crediting —
# harder is doing the block — only a sanity cap on identity (2×). Between the
# gray floor and the at-target floor is the band the recording cannot answer:
# a flat 85 % ride against a 105 % plan (0.81) and a forgotten lap that
# smeared a block with its recoveries (~0.70) both land here, and the session
# goes ungraded rather than guessed. Below the gray floor is clear absence —
# a 50 % soft-pedal where a block should be is a block not ridden.
LAP_BAND_UNDER = 0.90
# Coverage (grading a block with no anchor of its own) demands more than
# admissibility: a merged lap diluted to exactly 90 % of target is
# indistinguishable from a skip plus a hot neighbour, and it graded one
# "done". Identity can carry a 0.90 run; coverage alone cannot.
LAP_BAND_COVER = 0.95
LAP_BAND_GRAY = 0.60
LAP_BAND_OVER_X = 1.20

# Runs: adjacent laps at the same intensity are one effort (a double-tapped
# lap button splits a block without changing what was ridden).
LAP_RUN_MERGE_GAP_S = 3.0
LAP_RUN_MERGE_FRAC = 0.03

# Anchoring cost model. A step between anchors is charged for its drift
# RESIDUAL — how far the delivered gap differs from the prescribed one after
# allowing the pace this rider keeps (fitted from the session's own
# recoveries: delivered rest ≈ rho × prescribed + extra). A rider who
# stretches every recovery pays ~nothing; an anchor one leg out of place pays
# its displacement. Any number of blocks may be skipped between anchors —
# bounding that jump once made a six-block skip inexpressible and slid the
# whole report by a slot.
LAP_GAP_COST_W = 60.0
LAP_GAP_SOFTEN_S = 120.0
LAP_REST_SLOP = 0.10            # residual tolerance grows with the rest a step
                                # spans (pace-fit error is proportional to it)
LAP_RESID_POS_CAP = 480.0       # a LONGER gap than expected is life — a stop
                                # at the lights, a gel, a phone call — and one
                                # stop is one event however long it ran. A
                                # SHORTER gap than prescribed is not: that is
                                # what sliding the session onto the wrong runs
                                # looks like, and it pays in full.
LAP_DRIFT_PASS_W = 0.4          # gap weight while measuring the rider's pace
LAP_ORIGIN_W_NEG = 0.3          # first anchor EARLY: mostly a shorter warm-up
LAP_ORIGIN_W_POS = 1.0          # first anchor LATE: charged in full — sliding
                                # the whole session onto later runs starts here
LAP_BLOCK_WINDOW = 16           # skipped blocks allowed per step (a bound only
                                # so the DP terminates on absurd inputs)
LAP_ADM_TRIGGER = 40            # cap engages only past this — a normal
                                # session never reaches it
LAP_ADM_CAP = 12                # admissible runs considered per block, by
                                # distance from where the block was prescribed.
                                # Any number of runs may sit between adjacent
                                # anchors — a prescribed tempo section between
                                # the main set and the finisher blocks is ten
                                # runs of plan the alignment must hop in one
                                # step; an index window here forced the slide
                                # it existed to prevent.
LAP_REST_RATIO_MIN = 0.5        # pace-fit clamps
LAP_REST_RATIO_MAX = 5.0
LAP_REST_EXTRA_MIN = -120.0
LAP_REST_EXTRA_MAX = 1800.0

# How far from a missed block's window unclaimed at-target work may sit before
# the miss is judged unsettled (the mapping may be off by a leg).
LAP_NEAR_FRAC = 0.75
LAP_NEAR_S = 60.0
# A recovery ridden at block intensity AND materially over what the plan put
# there erases the boundary the grading depends on.
LAP_HOT_MARGIN = 0.10
# A run that could be SOME block but is claimed by none, sitting after the
# first anchor, is evidence against the alignment that strands it — without
# this charge a mid-ride pause was cheaper to explain by sliding every later
# block one run over (stranding the last run) than by paying for the pause.
LAP_ORPHAN_W = 0.5
# A single anchor far from where its block was prescribed is not a mapping —
# it is one lap of the right shape somewhere in a ride. Kept only when it sits
# near its block; otherwise the session grades as unanchored.
LAP_LONE_ANCHOR_S = 60.0


def _attr_f(el, name) -> "float | None":
    v = el.get(name)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_zwo_text(text: str) -> "list[dict]":
    """Parse ZWO XML text into an absolute segment timeline.

    Returns [{"kind", "start_s", "dur_s", "lo", "hi"}, ...] — see module
    docstring. Unknown tags and zero/absent durations are skipped.
    Raises ``xml.etree.ElementTree.ParseError`` on malformed XML.
    """
    root = ET.fromstring(text)
    workout = root.find(".//workout")
    if workout is None:
        return []
    segments: list[dict] = []
    t = 0

    def add(kind: str, dur: "float | None", lo: "float | None",
            hi: "float | None") -> None:
        nonlocal t
        d = int(round(dur or 0))
        if d <= 0:
            return
        segments.append({"kind": kind, "start_s": t, "dur_s": d,
                         "lo": lo, "hi": hi})
        t += d

    for el in workout:
        tag = el.tag.split("}")[-1]  # tolerate a namespaced document
        if tag in ("Warmup", "Cooldown", "Ramp"):
            add(tag.lower(), _attr_f(el, "Duration"),
                _attr_f(el, "PowerLow"), _attr_f(el, "PowerHigh"))
        elif tag == "SteadyState":
            p = _attr_f(el, "Power")
            add("steady", _attr_f(el, "Duration"), p, p)
        elif tag == "IntervalsT":
            reps = int(_attr_f(el, "Repeat") or 0)
            on_d = _attr_f(el, "OnDuration")
            off_d = _attr_f(el, "OffDuration")
            on_p = _attr_f(el, "OnPower")
            off_p = _attr_f(el, "OffPower")
            for _ in range(max(0, reps)):
                add("interval_on", on_d, on_p, on_p)
                add("interval_off", off_d, off_p, off_p)
        elif tag == "FreeRide":
            add("freeride", _attr_f(el, "Duration"), None, None)
        # anything else: ignore (no other tags exist in the library)
    return segments


def parse_zwo_file(path) -> "list[dict] | None":
    """I/O convenience wrapper: parse a .zwo file; None if unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_zwo_text(fh.read())
    except (OSError, ET.ParseError, UnicodeDecodeError):
        return None


def _seg_frac_at(seg: dict, i: int) -> "float | None":
    """Target FTP-fraction at second ``i`` inside ``seg`` (ramp-aware)."""
    lo, hi = seg.get("lo"), seg.get("hi")
    if lo is None or hi is None:
        return None
    d = seg["dur_s"]
    if d <= 1 or lo == hi:
        return float(lo)
    return float(lo) + (float(hi) - float(lo)) * (i / (d - 1))


def _target_pairs(segments: "list[dict]", ftp: float) -> "list[tuple[int, float]]":
    """[(second, target_watts)] for every defined (non-FreeRide) second."""
    pairs: list[tuple[int, float]] = []
    for seg in segments:
        for i in range(seg["dur_s"]):
            f = _seg_frac_at(seg, i)
            if f is not None:
                pairs.append((seg["start_s"] + i, f * ftp))
    return pairs


def _best_offset(pairs: "list[tuple[int, float]]", watts: "list[float]") -> int:
    """Minimum-MAE global alignment offset (see module docstring, step 2)."""
    n = len(watts)
    if not pairs or n == 0:
        return 0
    min_overlap = max(60, len(pairs) // 2)
    best_off, best_mae = 0, None
    for off in range(-ALIGN_MAX_OFFSET_S, ALIGN_MAX_OFFSET_S + 1):
        total, count = 0.0, 0
        for t, tw in pairs:
            j = t + off
            if 0 <= j < n:
                total += abs(watts[j] - tw)
                count += 1
        if count < min_overlap:
            continue
        mae = total / count
        if (best_mae is None or mae < best_mae - 1e-9
                or (abs(mae - best_mae) <= 1e-9 and abs(off) < abs(best_off))):
            best_mae, best_off = mae, off
    return best_off if best_mae is not None else 0


def _clean_watts(watts) -> "list[float] | None":
    if not isinstance(watts, (list, tuple)) or not watts:
        return None
    out: list[float] = []
    for w in watts:
        try:
            out.append(max(0.0, float(w)))
        except (TypeError, ValueError):
            out.append(0.0)  # sensor gap / null sample = no power delivered
    return out


def score_structure(planned_segments, watts, ftp) -> "dict | None":
    """Grade the delivered 1 Hz trace against the prescribed timeline.

    Args:
        planned_segments: output of :func:`parse_zwo_text` (or same shape).
        watts: 1 Hz per-second power list (None/invalid samples count as 0).
        ftp: FTP in watts used to realize the .zwo fractions.

    Returns the result dict documented in the module docstring, or None
    when nothing can honestly be graded (no segments / no watts / bad FTP /
    no work segments). Pure + deterministic.
    """
    try:
        ftp_f = float(ftp)
    except (TypeError, ValueError):
        return None
    if ftp_f <= 0 or not isinstance(planned_segments, (list, tuple)):
        return None
    segs = [s for s in planned_segments
            if isinstance(s, dict) and s.get("dur_s", 0) > 0]
    trace = _clean_watts(watts)
    if not segs or trace is None:
        return None

    work = [s for s in segs
            if s.get("lo") is not None and s.get("hi") is not None
            and (float(s["lo"]) + float(s["hi"])) / 2.0 >= WORK_FLOOR_FRAC]
    if not work:
        return None  # nothing structural to grade (pure endurance timeline)

    offset = _best_offset(_target_pairs(segs, ftp_f), trace)
    n = len(trace)

    rows: list[dict] = []
    for idx, seg in enumerate(work):
        dur = seg["dur_s"]
        mid = (float(seg["lo"]) + float(seg["hi"])) / 2.0
        floor_w = min(WORK_FLOOR_FRAC, MISSING_TARGET_FRAC * mid) * ftp_f
        sampled_w: list[float] = []       # delivered watts inside the trace
        sampled_t: list[float] = []       # matching per-second targets
        below_floor = 0
        band_total = band_in = 0          # post-grace band accounting
        for i in range(dur):
            tw = _seg_frac_at(seg, i) * ftp_f
            j = seg["start_s"] + i + offset
            if not (0 <= j < n):
                continue                  # absent second (trace ended/short)
            w = trace[j]
            sampled_w.append(w)
            sampled_t.append(tw)
            if w < floor_w:
                below_floor += 1
            if i >= TRANSIENT_GRACE_S:
                band_total += 1
                if abs(w - tw) <= max(TOL_FRAC * tw, TOL_MIN_W):
                    band_in += 1
        absent = dur - len(sampled_w)
        missing = (absent + below_floor) > MISSING_BELOW_FLOOR_FRAC * dur
        mean_ratio = None
        if sampled_w and sum(sampled_t) > 0:
            mean_ratio = round(sum(sampled_w) / sum(sampled_t), 3)
        on_target = None
        if band_total > 0:
            on_target = round(band_in / band_total, 3)
        rows.append({"index": idx, "start_s": seg["start_s"], "dur_s": dur,
                     "target_frac": round(mid, 3), "mean_ratio": mean_ratio,
                     "on_target_frac": on_target, "missing": missing})

    delivered = [r for r in rows if not r["missing"]]
    n_presc, n_deliv = len(rows), len(delivered)
    on_vals = [r["on_target_frac"] for r in delivered
               if r["on_target_frac"] is not None]
    ratio_vals = [r["mean_ratio"] for r in delivered
                  if r["mean_ratio"] is not None]
    worst = min(rows, key=lambda r: (0.0 if r["missing"]
                                     else (r["on_target_frac"]
                                           if r["on_target_frac"] is not None
                                           else 0.0), r["index"]),
                default=None) if rows else None
    return {
        "reps_prescribed": n_presc,
        "reps_delivered": n_deliv,
        "rep_completion": round(n_deliv / n_presc, 3),
        "mean_on_target_pct": (round(100.0 * sum(on_vals) / len(on_vals), 1)
                               if on_vals else None),
        "mean_power_ratio": (round(sum(ratio_vals) / len(ratio_vals), 3)
                             if ratio_vals else None),
        "alignment_offset_s": offset,
        "worst_segment": dict(worst) if worst is not None else None,
        "segments": rows,
    }


# ── Lap-based block grading ─────────────────────────────────────────────────

def _prescribed_reps(planned_segments, ftp) -> "list[dict]":
    """Work reps from the prescription, in order, grouped into sets.

    A "set" boundary is a recovery gap materially longer than the in-set
    recoveries — that is what separates 3×13 from 39 straight reps, and it is
    how the rider thinks about the session ("10 of 13 in each set").
    """
    # WHICH segments are reps. The rider laps the WORK intervals, so the
    # prescription's own structure must decide — not an intensity floor.
    # A 30/15 float session has its OFF legs at 89 % FTP and its lead-in ramp
    # at 97 %, both above WORK_FLOOR_FRAC; grading those as reps mis-numbered
    # every block and compared a 30 s rep against a 180 s ramp. When the file
    # declares intervals (IntervalsT → "interval_on"), those ARE the reps and
    # nothing else is. Only files with no declared intervals fall back to
    # steady blocks above the floor (a tempo/threshold session lapped per
    # block).
    try:
        ftp = float(ftp) if ftp else None
    except (TypeError, ValueError):
        ftp = None                      # target_w is advisory; never raise here
    kinds = {seg.get("kind") for seg in planned_segments}
    explicit = "interval_on" in kinds

    def _is_rep(seg, mid) -> bool:
        # The intensity floor applies in BOTH modes: a warmup fast-pedal drill
        # is also an IntervalsT ("5 × 30 s @ 65 %"), and counting those as reps
        # numbered the blocks from the warmup instead of the main set.
        if mid is None or mid < WORK_FLOOR_FRAC:
            return False
        if explicit:
            return seg.get("kind") == "interval_on"
        return seg.get("kind") == "steady"

    reps: list[dict] = []
    gaps: list[float] = []
    prev_end = None
    for seg in planned_segments:
        mid = _seg_frac_at(seg, seg["dur_s"] // 2) if seg["dur_s"] else None
        if not _is_rep(seg, mid):
            continue
        if mid is None:
            continue
        if prev_end is not None:
            gaps.append(max(0.0, seg["start_s"] - prev_end))
        reps.append({"dur_s": seg["dur_s"], "target_frac": mid,
                     "target_w": (mid * ftp) if ftp else None,
                     "start_s": seg["start_s"]})
        prev_end = seg["start_s"] + seg["dur_s"]
    if not reps:
        return []
    # Set split: a gap ≥ 2× the median in-set gap (and ≥ 60 s) starts a new set.
    set_idx = 0
    if gaps:
        ordered = sorted(gaps)
        med = ordered[len(ordered) // 2] or 0.0
        thresh = max(60.0, 2.0 * med) if med else None
    else:
        thresh = None
    reps[0]["set"] = 0
    for i in range(1, len(reps)):
        g = gaps[i - 1] if i - 1 < len(gaps) else 0.0
        if thresh is not None and g >= thresh:
            set_idx += 1
        reps[i]["set"] = set_idx
    return reps


def _normalised_laps(laps, ftp) -> "list[dict] | None":
    """Every lap, in ride-clock order, as {t0, dur, frac, pct}.

    Returns None — grade nothing — rather than guess, when the lap list cannot
    carry the question:

    * a lap with no ``start_s``. Earlier versions recovered a clock by summing
      the durations of the laps before it, which is the same number only while
      the laps tile the ride with no gaps and the rider never stops. A pause
      slides every later lap and the summed clock never notices.
    * a lap of real length with no usable intensity. Every admissibility test
      below is an intensity test; without one the grader is matching on
      duration alone, which is how a 60 s soft-pedal came to stand in for a
      45 s VO2 rep.
    """
    if not isinstance(laps, (list, tuple)) or not laps:
        return None
    try:
        ftp_f = float(ftp) if ftp else None
    except (TypeError, ValueError):
        ftp_f = None
    out: list[dict] = []
    for lap in laps:
        if not isinstance(lap, dict):
            return None
        try:
            dur = float(lap.get("duration_s") or 0)
        except (TypeError, ValueError):
            return None
        if dur <= 0:
            continue
        t0 = lap.get("start_s")
        if t0 is None:
            return None
        try:
            t0 = float(t0)
        except (TypeError, ValueError):
            return None
        pct = lap.get("ftp_pct")
        try:
            frac = float(pct) / 100.0 if pct is not None else None
        except (TypeError, ValueError):
            frac = None
        if frac is None and ftp_f:
            try:
                w = lap.get("avg_power_w")
                frac = float(w) / ftp_f if w is not None else None
            except (TypeError, ValueError, ZeroDivisionError):
                frac = None
        if frac is None:
            if dur >= LAP_TRIVIAL_LAP_S:
                return None
            continue                    # a 1 s tap with no power: ignorable
        out.append({"t0": t0, "dur": dur, "frac": frac,
                    "pct": round(frac * 100.0, 1),
                    "work": (str(lap.get("type") or "").strip().upper() == "WORK"
                             or frac >= WORK_FLOOR_FRAC)})
    out.sort(key=lambda r: r["t0"])
    return out



# ── Runs: the delivered timeline in graded units ────────────────────────────

def _runs(all_laps) -> "list[dict]":
    """Adjacent laps at the same intensity, merged into one effort.

    A double-tapped lap button splits one block into two laps of identical
    intensity back to back; the run is the effort the rider actually made.
    Laps at different intensities never merge — a block and the float after
    it are two runs — and a gap in the tiling (a stop with the recorder off)
    ends a run.
    """
    runs: list[dict] = []
    for lap in all_laps:
        if runs:
            prev = runs[-1]
            if (lap["t0"] - prev["end"] <= LAP_RUN_MERGE_GAP_S
                    and abs(lap["frac"] - prev["frac"]) <= LAP_RUN_MERGE_FRAC):
                d = prev["dur"] + lap["dur"]
                prev["frac"] = (prev["frac"] * prev["dur"]
                                + lap["frac"] * lap["dur"]) / d
                prev["dur"] = d
                prev["end"] = lap["t0"] + lap["dur"]
                prev["pct"] = round(prev["frac"] * 100.0, 1)
                continue
        runs.append({"t0": lap["t0"], "dur": lap["dur"],
                     "end": lap["t0"] + lap["dur"],
                     "frac": lap["frac"], "pct": lap["pct"]})
    return runs


def _pairing(rep, run) -> "float | None":
    """Value of reading ``run`` as the block ``rep`` — or None, not this block.

    A run materially UNDER the block's target cannot be that block: an
    over/under's 90 % leg is not its 105 % block, and a flat 85 % tempo ride is
    not a VO2 session however its head unit lapped it. There is deliberately no
    upper duration bound — a block held longer than asked was still ridden, and
    vetoing it reported the rider as having SKIPPED the block they overdid.
    """
    tgt = max(float(rep["target_frac"]), 0.01)
    presc = max(float(rep["dur_s"]), 1.0)
    if run["frac"] < LAP_BAND_UNDER * tgt:
        return None
    if run["frac"] > LAP_BAND_OVER_X * tgt:
        return None
    if run["dur"] < LAP_TRIVIAL_FRAC * presc:
        return None
    # A run much LONGER than the block and UNDER its target is not the block
    # ridden long — riding long happens at target. It is the signature of a
    # skip smeared together with a hot neighbour, whose mix can land exactly
    # on the admissibility edge.
    if run["dur"] > 1.25 * presc and run["frac"] < 0.95 * tgt:
        return None
    q_int = max(0.0, 1.0 - abs(run["frac"] - tgt) / tgt)
    # The flat term keeps a 10-second sprint's anchor worth claiming against
    # honest clock noise — position error is not proportional to block
    # length, so the value of pinning the clock is not either.
    return (min(run["dur"], presc) + 20.0) * (0.25 + 0.75 * q_int)


# ── Anchoring: which run IS which block, where that is certain ──────────────

def _anchor(reps, runs, pace, bands, segn, holes=(), weight=1.0) -> "list[int | None]":
    """Order-preserving assignment of runs to blocks, for the time MAPPING.

    This no longer decides any block's fate — statuses come from the timeline
    (:func:`_grade`). It only pins down where the rider's clock sits against
    the plan's, so it needs to be right where it is confident and absent where
    it is not; an unanchored block is graded from its predicted window.

    Score: the pairing value less a charge for the step's drift RESIDUAL —
    how far the gap from the previous anchor differs from the prescribed gap
    after allowing the pace this rider is keeping (``pace`` = (rho, extra):
    delivered rest ≈ rho × prescribed rest + extra per gap, fitted from the
    session itself). A rider who stretches every recovery pays ~nothing; an
    anchor one leg out of place pays its full displacement.

    Any number of blocks may be skipped between anchors (a rider who
    soft-pedals six blocks mid-session resumes at block nine — a bounded jump
    made that inexpressible and slid the whole report by a slot). The run
    window only limits how many spare runs sit between anchors.
    """
    n, m = len(reps), len(runs)
    if not n or not m:
        return [None] * n, 0.0
    rho, extra = pace
    lw = LAP_GAP_COST_W * weight
    S = LAP_GAP_SOFTEN_S

    adm: "list[list[tuple[int, float]]]" = []
    for i, rep in enumerate(reps):
        row = [(j, v) for j in range(m)
               if (v := _pairing(rep, runs[j])) is not None]
        if len(row) > LAP_ADM_TRIGGER:
            # Plausibility cap, so a wall of stray admissible runs cannot turn
            # the alignment quadratic. By ORDINAL among NEAR-TARGET runs, not
            # by clock distance and not by list position: the k-th block of a
            # given intensity is ridden as roughly the k-th run NEAR that
            # intensity, however far the rider's recoveries have pushed the
            # clock. A distance cap dropped the true runs of every late block
            # in a long slow session; a list-position cap spent the budget on
            # admissible-but-off-target runs (a session's hot finisher taps)
            # and did the same. Off-target and clock-nearest entries are kept
            # in small fixed numbers.
            tgt = float(rep["target_frac"])
            st = float(rep["start_s"])
            near = [jv for jv in row
                    if abs(runs[jv[0]]["frac"] - tgt) <= 0.15 * tgt]
            far = [jv for jv in row
                   if abs(runs[jv[0]]["frac"] - tgt) > 0.15 * tgt]
            rank = sum(1 for k in range(i)
                       if abs(float(reps[k]["target_frac"]) - tgt)
                       <= 0.10 * tgt)
            half = LAP_ADM_CAP
            keep = {jv[0] for jv in near[max(0, rank - half):rank + half + 1]}
            keep.update(jv[0] for jv in sorted(
                near, key=lambda jv: abs(runs[jv[0]]["t0"] - st))[:4])
            keep.update(jv[0] for jv in sorted(
                far, key=lambda jv: abs(runs[jv[0]]["t0"] - st))[:4])
            row = [jv for jv in row if jv[0] in keep]
        adm.append(row)
    if not any(adm):
        return [None] * n, 0.0
    run_is_adm = [False] * m
    for row in adm:
        for j, _v in row:
            run_is_adm[j] = True
    # A run whose intensity the plan prescribes SOMEWHERE outside the blocks
    # is not evidence when stranded — the rider may simply have ridden that
    # part of the plan. Charging it pushed anchors onto a sweet-spot file's
    # own tempo section.
    def _plan_explains(frac: float) -> bool:
        t = _band_tol(frac)
        return any(lo - t <= frac <= hi + t for _a, _b, lo, hi in bands)

    strand = [run_is_adm[j] and not _plan_explains(runs[j]["frac"])
              for j in range(m)]
    admdur = [0.0] * (m + 1)      # prefix sum of strandable-run seconds
    for j in range(m):
        admdur[j + 1] = admdur[j] + (runs[j]["dur"] if strand[j] else 0.0)

    starts = [float(r["start_s"]) for r in reps]
    durs = [float(r["dur_s"]) for r in reps]
    cumdur = [0.0] * (n + 1)
    for i in range(n):
        cumdur[i + 1] = cumdur[i] + durs[i]

    def step_cost(i1, j1, i2, j2) -> float:
        deliv = runs[j2]["t0"] - runs[j1]["t0"]
        # A gap in the lap tiling is the recorder switched off — a pause.
        # That time did not happen as far as the workout is concerned, so it
        # is not evidence of anything; without this, a ten-minute stop looked
        # exactly like ten minutes of skipped plan.
        for h_lo, h_hi in holes:
            if runs[j1]["end"] <= h_lo and h_hi <= runs[j2]["t0"]:
                deliv -= (h_hi - h_lo)
        block = cumdur[i2] - cumdur[i1]          # blocks i1..i2-1
        rest = max(0.0, starts[i2] - starts[i1] - block)
        expect = block + rho * rest + extra * (segn[i2] - segn[i1])
        resid = deliv - max(block, expect)
        if resid > 0:
            resid = min(resid, LAP_RESID_POS_CAP)
        gap = lw * abs(resid) / (S + LAP_REST_SLOP * rest)
        # Admissible runs this step strands, charged at LAP_ORPHAN_W.
        return gap + LAP_ORPHAN_W * (admdur[j2] - admdur[j1 + 1])

    # Admissible runs that start at-or-after the first block's prescribed
    # start: stranding one of these BEFORE the first anchor is the signature
    # of the whole session sliding one run over (the pause exploit). Runs
    # before that point are the warm-up and stay free.
    late = [strand[j] and runs[j]["t0"] >= starts[0] - 30.0
            for j in range(m)]
    latedur = [0.0] * (m + 1)
    for j in range(m):
        latedur[j + 1] = latedur[j] + (runs[j]["dur"] if late[j] else 0.0)

    def origin_cost(i, j) -> float:
        d = runs[j]["t0"] - starts[i]
        w = LAP_ORIGIN_W_POS if d > 0 else LAP_ORIGIN_W_NEG
        return (w * lw * abs(d) / (starts[i] + S)
                + LAP_ORPHAN_W * latedur[j])

    # F[(i, j)] = best score of an alignment whose LAST anchor reads block i
    # from run j. States are only the admissible pairs — tight bands plus the
    # positional cap keep this small even on a 96-block session.
    F: dict = {}
    back: dict = {}
    order = [(i, j, v) for i in range(n) for (j, v) in adm[i]]
    for i, j, v in order:
        best, bp = v - origin_cost(i, j), None
        for i1 in range(max(0, i - LAP_BLOCK_WINDOW), i):
            for j1, _v1 in adm[i1]:
                if j1 >= j:
                    continue
                f1 = F.get((i1, j1))
                if f1 is None:
                    continue
                cand = f1 + v - step_cost(i1, j1, i, j)
                if cand > best:
                    best, bp = cand, (i1, j1)
        F[(i, j)] = best
        back[(i, j)] = bp
    best_state, best_val = None, 0.0
    for st, val in F.items():
        # Admissible runs stranded AFTER the last anchor count too — the same
        # evidence, at the same price.
        val = val - LAP_ORPHAN_W * (admdur[m] - admdur[st[1] + 1])
        if val > best_val:
            best_state, best_val = st, val
    out: "list[int | None]" = [None] * n
    st = best_state
    while st is not None:
        out[st[0]] = st[1]
        st = back[st]
    return out, best_val


def _fit_pace(reps, runs, anchors, segn) -> "tuple[float, float]":
    """(rho, extra): delivered rest ≈ rho × prescribed rest + extra per easy
    SEGMENT of the plan, fitted by median over the anchored steps. Per
    segment, not per block: a rider stretches each recovery they take, and a
    gap holding two easy segments stretches twice. This is how a rider who
    takes half again as long between every block, or a flat extra minute at
    each one, costs nothing — the stretch is the session's own norm."""
    pts = []
    loose = []
    prev = None
    for i, j in enumerate(anchors):
        if j is None:
            continue
        if prev is not None:
            i1, j1 = prev
            block = sum(float(reps[k]["dur_s"]) for k in range(i1, i))
            rest = max(0.0, float(reps[i]["start_s"])
                       - float(reps[i1]["start_s"]) - block)
            deliv_rest = runs[j]["t0"] - runs[j1]["t0"] - block
            nseg = max(1, segn[i] - segn[i1])
            if rest >= 5.0:
                loose.append((rest, deliv_rest, nseg))
                # Steps between ADJACENT blocks only: a step spanning skipped
                # blocks measures the skip, not the rider's pace, and one bad
                # first-pass alignment then poisons the model the second pass
                # corrects with.
                if i - i1 == 1:
                    pts.append((rest, deliv_rest, nseg))
        prev = (i, j)
    if len(pts) < 2:
        pts = loose
    if len(pts) < 3:
        # One or two steps cannot carry a two-parameter model — on a
        # three-block session with a skip, the fit was pure noise and the
        # noise then decided which block "moved".
        return 1.0, 0.0

    def med(xs):
        xs = sorted(xs)
        h = len(xs) // 2
        return xs[h] if len(xs) % 2 else 0.5 * (xs[h - 1] + xs[h])

    cands = [(1.0, med([(b - r) / g for r, b, g in pts])),
             (min(LAP_REST_RATIO_MAX,
                  max(LAP_REST_RATIO_MIN,
                      med([b / r for r, b, _g in pts]))), 0.0)]
    slopes = [(b2 - b1) / (r2 - r1) for x, (r1, b1, _s1) in enumerate(pts)
              for r2, b2, _s2 in pts[x + 1:] if abs(r2 - r1) >= 5.0]
    if slopes:
        rho = min(LAP_REST_RATIO_MAX, max(LAP_REST_RATIO_MIN, med(slopes)))
        cands.append((rho, med([(b - rho * r) / g for r, b, g in pts])))
    best = min(cands, key=lambda c: sum(
        abs(b - (c[0] * r + c[1] * g)) for r, b, g in pts) / len(pts))
    return best[0], min(LAP_REST_EXTRA_MAX,
                        max(LAP_REST_EXTRA_MIN, best[1]))


# ── Grading: what the timeline shows in each block's window ─────────────────

def _windows(reps, runs, anchors) -> "list[tuple[float, float]]":
    """Each block's expected place on the ride clock.

    An anchored block's window is its own run's start — exact. An unanchored
    block interpolates the drift of the anchors around it (a rider who is
    three minutes behind at block 4 and four at block 7 is ~3.3 behind at
    block 5), holding the nearest anchor's drift before the first and after
    the last. This is what makes a mid-session skip land where it happened
    instead of sliding every later block by one.
    """
    n = len(reps)
    drift: "list[float | None]" = [None] * n
    for i, j in enumerate(anchors):
        if j is not None:
            drift[i] = runs[j]["t0"] - float(reps[i]["start_s"])
    idx = [i for i in range(n) if drift[i] is not None]
    for i in range(n):
        if drift[i] is not None:
            continue
        before = [k for k in idx if k < i]
        after = [k for k in idx if k > i]
        if before and after:
            a, b = before[-1], after[0]
            ta, tb = float(reps[a]["start_s"]), float(reps[b]["start_s"])
            t = float(reps[i]["start_s"])
            w = (t - ta) / (tb - ta) if tb > ta else 0.0
            drift[i] = drift[a] + w * (drift[b] - drift[a])
        elif before:
            drift[i] = drift[before[-1]]
        elif after:
            drift[i] = drift[after[0]]
        else:
            drift[i] = 0.0
    return [(float(r["start_s"]) + drift[i],
             float(r["start_s"]) + drift[i] + float(r["dur_s"]))
            for i, r in enumerate(reps)]


def _overlap(lo1, hi1, lo2, hi2) -> float:
    return max(0.0, min(hi1, hi2) - max(lo1, lo2))


def score_blocks(planned_segments, laps, ftp=None) -> "dict | None":
    """Grade which prescribed BLOCKS the rider actually completed, from laps.

    Answers the question a load number cannot: "I stopped early — which blocks
    did I do?" Laps carry the offset on the ride clock where each one started
    (persisted from intervals.icu), so the delivered ride is a timeline the
    prescription can be checked against.

    The rule that survived six generations of this feature: a block gets a
    verdict on IDENTITY or on PROOF OF ABSENCE, and on nothing else.

    * Identity: an anchored block — its own run, at its intensity, where the
      session's clock puts it — is ridden, and its run's length says done or
      partial. Over-length and over-intensity stay "done": harder or longer
      is still doing the block.
    * Proof of absence: an unanchored block is "missed" only when its window
      shows nothing but clearly-easy riding (below 60 % of the block's
      target) or lies beyond the end of the ride, AND no unclaimed run
      anywhere order-consistent could have been the block.
    * Anything else — a window in the gray band, a spare run that fits, a
      clock that reads two ways — is a question the recording does not
      answer, and the whole session goes ungraded. A missing report is
      recoverable; a false green tick is not.

    Returns None whenever the laps do not determine the answer. Result::

      {
        "outcome":        "completed"    — every block, full length
                          | "short_blocks" — every block, some ran short
                          | "cut_short"    — stopped; all gaps at the end
                          | "off_plan"     — gap(s) in the middle
                          | "not_attempted"— nothing materially delivered,
        "reps_prescribed": int,
        "reps_done":       int,   # delivered at ~full prescribed duration
        "reps_partial":    int,   # started but materially short
        "reps_missed":     int,
        "work_fraction":   float, # delivered work seconds / prescribed
        "stopped_after":   int|None,   # 1-based rep index the rider got to
        "sets":            list[dict], # per set: prescribed/done/partial
        "reps":            list[dict], # per rep: index, set, status, …
        "basis":           "laps",
      }
    """
    try:
        reps = _prescribed_reps(planned_segments or [], ftp)
        all_laps = _normalised_laps(laps, ftp)
        if not reps or not all_laps:
            return None
        runs = _runs(all_laps)
        bands = _plan_bands(planned_segments, reps)
        rep_starts = {r["start_s"] for r in reps}
        easy_starts = sorted(
            float(seg["start_s"]) for seg in (planned_segments or [])
            if (seg.get("dur_s") or 0)
            and seg.get("start_s") not in rep_starts)
        import bisect as _bisect
        segn = [_bisect.bisect_left(easy_starts, float(r["start_s"]))
                for r in reps]
        holes = []
        for l1, l2 in zip(all_laps, all_laps[1:]):
            gap = l2["t0"] - (l1["t0"] + l1["dur"])
            if gap > 45.0:
                holes.append((l1["t0"] + l1["dur"], l2["t0"]))

        anchors, _sc = _anchor(reps, runs, (1.0, 0.0), bands, segn, holes,
                               LAP_DRIFT_PASS_W)
        pace2 = (1.0, 0.0)
        for _ in range(4):
            pace2 = _fit_pace(reps, runs, anchors, segn)
            again, _sc = _anchor(reps, runs, pace2, bands, segn, holes)
            if again == anchors:
                break
            anchors = again
        else:
            # No fixed point: the reading depends on where you start
            # measuring from — the laps do not determine it.
            return None

        # An anchor whose run sits where the PLAN put a non-block segment of
        # that intensity is the rider riding that part of the plan, not a
        # block. The plan's material sits at the SESSION'S drift, not at
        # zero: a rider who cut the warm-up short moved every prescribed
        # segment with them. Zero is still tested while the session is near
        # schedule — a rider who quit during the warm-up has no honest
        # drift, only fake anchors defining one. Scrubbing can un-slide the
        # chain behind it, so the alignment is re-solved without the
        # scrubbed runs until it settles.
        def _explained_here(run) -> bool:
            """Band-explained at the run's OWN position: the rider riding that
            part of the plan, on schedule."""
            t = _band_tol(run["frac"])
            for a, b, lo, hi in bands:
                if not (lo - t <= run["frac"] <= hi + t):
                    continue
                if _overlap(a, b, run["t0"], run["end"]) < 0.5 * run["dur"]:
                    continue
                # The run must plausibly BE that segment — a 2-minute block
                # run is not the 10-minute tempo section it happens to sit
                # inside once the session has drifted.
                if run["dur"] >= 0.4 * (b - a):
                    return True
            return False

        # An anchor whose run is the plan's own non-block material, ridden on
        # schedule, is the rider following the plan — not a block. But a
        # whole session legitimately shifts (a short warm-up moves every true
        # block run onto some earlier segment's old position), so being
        # explained is only damning when the anchor's drift DISAGREES with
        # the session's (it reached for material the rest of the ride says is
        # not where its block went), or when most anchors are explained (no
        # honest session is mostly plan-material — that is a warm-up ridden
        # and a session abandoned). Scrubbing can un-slide the chain behind
        # it, so the alignment is re-solved without the scrubbed runs.
        banned: set = set()
        for _ in range(3):
            placed_now = [(i, j) for i, j in enumerate(anchors)
                          if j is not None]
            if not placed_now:
                break
            drifts = sorted(runs[j]["t0"] - float(reps[i]["start_s"])
                            for i, j in placed_now)
            med = drifts[len(drifts) // 2]
            expl = {j: _explained_here(runs[j]) for _i, j in placed_now}
            mostly = sum(expl.values()) >= 0.5 * len(placed_now)
            dirty = False
            for i, j in placed_now:
                if not expl[j]:
                    continue
                outlier = abs((runs[j]["t0"] - float(reps[i]["start_s"]))
                              - med) > 90.0 + 0.25 * float(reps[i]["dur_s"])
                if outlier or mostly:
                    banned.add(j)
                    dirty = True
            if not dirty:
                break
            live = [r if k not in banned else {**r, "frac": -1.0}
                    for k, r in enumerate(runs)]
            anchors, _sc = _anchor(reps, live, pace2, bands, segn, holes)

        placed = [(i, j) for i, j in enumerate(anchors) if j is not None]
        if len(placed) == 1:
            i, j = placed[0]
            tol = 0.5 * LAP_LONE_ANCHOR_S + 0.25 * float(reps[i]["dur_s"])
            if abs(runs[j]["t0"] - float(reps[i]["start_s"])) > tol \
                    or runs[j]["dur"] < 0.5 * float(reps[i]["dur_s"]):
                anchors = [None] * len(reps)
                placed = []
        if not placed:
            # No mapping at all. Grade only when the ride plainly contains
            # nothing that could have been a block — a rider who quit in the
            # warm-up after a hot lead-in effort gets silence, not a verdict
            # hung on one unplaced lap.
            if any(_pairing(rep, run) is not None
                   for rep in reps for run in runs):
                return None

        # A block bordered by prescribed material AT ITS OWN INTENSITY has no
        # boundary in the plan. It can be graded by IDENTITY (the rider's own
        # lap press marks it); left unanchored, no recording can say whether
        # it was ridden, cut short, or skipped.
        for i, rep in enumerate(reps):
            if anchors[i] is not None:
                continue
            tgt = max(float(rep["target_frac"]), 0.01)
            presc = max(float(rep["dur_s"]), 1.0)
            n_lo = float(rep["start_s"]) - presc
            n_hi = float(rep["start_s"]) + 2.0 * presc
            for a_b, b_b, lo_b, hi_b in bands:
                if _overlap(a_b, b_b, n_lo, n_hi) <= 0.0:
                    continue
                if lo_b - 0.10 * tgt <= tgt <= hi_b + 0.10 * tgt:
                    return None

        wins = _windows(reps, runs, anchors)
        ride_end = max(l["t0"] + l["dur"] for l in all_laps)

        n = len(reps)
        rows: list[dict] = []
        done = partial = missed = 0
        deliv_s = 0.0
        presc_s = float(sum(r["dur_s"] for r in reps))

        for i, rep in enumerate(reps):
            tgt = max(float(rep["target_frac"]), 0.01)
            presc = max(float(rep["dur_s"]), 1.0)
            lo, hi = wins[i]
            j = anchors[i]
            row = {"index": i + 1, "set": rep.get("set", 0) + 1,
                   "prescribed_s": rep["dur_s"],
                   "target_pct": round(100.0 * tgt, 1),
                   "delivered_s": None, "delivered_pct": None,
                   "on_target": None, "status": "missed"}

            if j is not None:
                run = runs[j]
                cov = min(run["dur"], presc) / presc
                row["delivered_s"] = int(run["dur"])
                row["delivered_pct"] = run["pct"]
                row["on_target"] = bool(
                    abs(run["frac"] - tgt) <= LAP_POWER_TOL_FRAC * tgt)
                deliv_s += min(run["dur"], presc)
                if cov >= LAP_COV_DONE:
                    row["status"] = "done"
                    done += 1
                elif cov >= LAP_TRIVIAL_FRAC:
                    row["status"] = "partial"
                    partial += 1
                else:
                    row["status"] = "missed"
                    missed += 1
            else:
                # Unanchored: only PROVEN absence grades. Every second of
                # the window must be beyond the ride or under clearly-easy
                # riding; one second in the gray band and the session is
                # silent.
                for lap in all_laps:
                    ov = _overlap(lo, hi, lap["t0"], lap["t0"] + lap["dur"])
                    # A stray tap of a few seconds is not evidence the block
                    # was ridden — the double-tap after stopping at block 8
                    # must not turn blocks 9 and 10 into open questions.
                    if ov < max(LAP_TRIVIAL_LAP_S, 0.15 * presc):
                        continue
                    if lap["frac"] >= LAP_BAND_GRAY * tgt:
                        return None
                covered = sum(
                    _overlap(lo, hi, lap["t0"], lap["t0"] + lap["dur"])
                    for lap in all_laps)
                beyond = max(0.0, hi - max(lo, ride_end))
                if covered + beyond < 0.8 * presc:
                    # A stretch of the window is unaccounted for — a hole in
                    # the recording is not proof of anything.
                    if beyond <= 0.0:
                        return None
                row["status"] = "missed"
                missed += 1
            rows.append(row)

        # A block called unridden while the LAP over its window is energy-rich
        # enough to CONTAIN it is a forgotten lap-button press, not a skip: a
        # 4-minute lap at 72 % where the plan says two 15-second sprints,
        # their rest and a 65 % lead-in holds exactly the sprints' energy, and
        # whether they were ridden is not in the recording. A lap at the
        # skipped-expectation's own intensity is a genuine skip and stays
        # missed — the energy is measurably absent.
        for i, row in enumerate(rows):
            if row["status"] != "missed":
                continue
            rep = reps[i]
            tgt = max(float(rep["target_frac"]), 0.01)
            presc = max(float(rep["dur_s"]), 1.0)
            lo, hi = wins[i]
            d_i = lo - float(rep["start_s"])
            for lap in all_laps:
                ov = _overlap(lo, hi, lap["t0"], lap["t0"] + lap["dur"])
                if ov < max(LAP_TRIVIAL_LAP_S, 0.15 * presc):
                    continue
                if lap["frac"] >= LAP_BAND_UNDER * tgt:
                    continue
                mu_rest = _plan_rest_frac(planned_segments, reps,
                                          lap["t0"] - d_i,
                                          lap["t0"] + lap["dur"] - d_i)
                span = max(lap["dur"], presc)
                mu_with = (presc * tgt
                           + max(0.0, span - presc) * mu_rest) / span
                if mu_with - mu_rest < 0.03:
                    return None       # the two stories are indistinguishable
                if lap["frac"] >= mu_rest + 0.6 * (mu_with - mu_rest):
                    return None

        # A block called unridden while a run that could have BEEN it sits
        # claimed by nothing, order-consistent with the anchors around it, is
        # not a settled question — the alignment may simply have slid. This
        # gate deliberately does NOT reuse the identity band: a run vetoed
        # for being too hot is exactly the kind the gate exists to see (the
        # block ridden 40 % over is not "missed" — it is unsettled). Runs the
        # plan itself explains, at their own position or at the session's
        # drift, are the rider riding the plan and prove nothing.
        claimed_j = {j for j in anchors if j is not None}
        anchor_ds = sorted(runs[j]["t0"] - float(reps[i]["start_s"])
                           for i, j in enumerate(anchors) if j is not None)
        sess_d = anchor_ds[len(anchor_ds) // 2] if anchor_ds else 0.0

        def _plan_material(run) -> bool:
            if _explained_here(run):
                return True
            if abs(sess_d) < 30.0:
                return False
            shifted = {**run, "t0": run["t0"] - sess_d,
                       "end": run["end"] - sess_d}
            return _explained_here(shifted)

        for i, row in enumerate(rows):
            if row["status"] != "missed":
                continue
            tgt = max(float(reps[i]["target_frac"]), 0.01)
            presc = max(float(reps[i]["dur_s"]), 1.0)
            prev_t = max((runs[anchors[k]]["end"] for k in range(i)
                          if anchors[k] is not None), default=float("-inf"))
            next_t = min((runs[anchors[k]]["t0"] for k in range(i + 1, n)
                          if anchors[k] is not None), default=float("inf"))
            for j, run in enumerate(runs):
                if j in claimed_j:
                    continue
                if run["frac"] < LAP_BAND_UNDER * tgt \
                        or run["dur"] < LAP_TRIVIAL_FRAC * presc:
                    continue
                if not (prev_t - LAP_NEAR_S <= run["t0"]
                        <= next_t + LAP_NEAR_S):
                    continue
                if _plan_material(run):
                    continue
                return None

        # A rider who rode the RECOVERIES at block intensity erased the
        # boundaries the grading depends on: a steady threshold hour against
        # an interval file covers every window without the pattern ever
        # being executed. "Hot" is relative to what the recovery PRESCRIBED.
        hot = 0
        checked = 0
        for i in range(n - 1):
            if reps[i].get("set") != reps[i + 1].get("set"):
                continue
            g_lo, g_hi = wins[i][1], wins[i + 1][0]
            if g_hi - g_lo < 5.0:
                continue
            checked += 1
            presc_gap_frac = _prescribed_gap_frac(planned_segments, reps, i)
            tgt = min(float(reps[i]["target_frac"]),
                      float(reps[i + 1]["target_frac"]))
            hot_secs = 0.0
            for run in runs:
                ov = _overlap(g_lo, g_hi, run["t0"], run["end"])
                if ov <= 0.0:
                    continue
                if (run["frac"] >= LAP_BAND_UNDER * tgt
                        and run["frac"] >= presc_gap_frac + LAP_HOT_MARGIN):
                    hot_secs += ov
            if hot_secs >= 0.6 * (g_hi - g_lo):
                hot += 1
        if checked and hot >= max(2, int(0.34 * checked) + 1):
            return None

        # Where did the rider get to? Last rep with any delivery.
        stopped_after = None
        for row in rows:
            if row["status"] in ("done", "partial"):
                stopped_after = row["index"]

        if done + partial == 0:
            outcome = "not_attempted"
        elif missed == 0 and partial == 0:
            outcome = "completed"
        elif missed == 0:
            outcome = "short_blocks"
        elif stopped_after is not None and stopped_after < n and all(
                r["status"] == "missed" for r in rows[stopped_after:]):
            outcome = "cut_short"
        else:
            outcome = "off_plan"

        sets: dict[int, dict] = {}
        for row in rows:
            st = sets.setdefault(row["set"], {"set": row["set"],
                                              "prescribed": 0, "done": 0,
                                              "partial": 0, "missed": 0,
                                              "ridden": 0})
            st["prescribed"] += 1
            st[row["status"]] = st.get(row["status"], 0) + 1
            if row["status"] in ("done", "partial"):
                st["ridden"] += 1

        return {
            "outcome": outcome,
            "reps_prescribed": n,
            "reps_done": done,
            "reps_partial": partial,
            "reps_missed": missed,
            "work_fraction": round(deliv_s / presc_s, 3) if presc_s else None,
            "stopped_after": stopped_after,
            "sets": [sets[k] for k in sorted(sets)],
            "reps": rows,
            "basis": "laps",
        }
    except Exception:
        # Grading is advisory; a shape this code does not understand must
        # read as "no verdict", never as a crash in the ride view.
        return None


def _prescribed_gap_frac(planned_segments, reps, i) -> float:
    """Highest intensity the PLAN itself puts between block i and block i+1."""
    lo = float(reps[i]["start_s"]) + float(reps[i]["dur_s"])
    hi = float(reps[i + 1]["start_s"])
    out = 0.0
    for seg in (planned_segments or []):
        d = seg.get("dur_s") or 0
        st = float(seg.get("start_s") or 0)
        if not d or st + d <= lo or st >= hi:
            continue
        f = _seg_frac_at(seg, int(d) // 2)
        if f is not None and f > out:
            out = float(f)
    return out


def _plan_rest_frac(planned_segments, reps, lo: float, hi: float) -> float:
    """Time-weighted prescribed intensity over plan interval [lo, hi],
    counting non-block segments only (block seconds contribute nothing —
    they are the thing whose presence is being tested)."""
    rep_starts = {r["start_s"] for r in reps}
    acc = dur = 0.0
    for seg in (planned_segments or []):
        d = seg.get("dur_s") or 0
        st = float(seg.get("start_s") or 0)
        if not d or st + d <= lo or st >= hi:
            continue
        ov = min(hi, st + d) - max(lo, st)
        if seg.get("start_s") in rep_starts:
            dur += ov          # block time inside the span, at zero
            continue
        f = _seg_frac_at(seg, int(d) // 2)
        acc += ov * (f if f is not None else 0.5)
        dur += ov
    return acc / dur if dur > 0 else 0.5


def _plan_bands(planned_segments, reps) -> "list[tuple[float, float, float, float]]":
    """(start_s, end_s, lo_frac, hi_frac) of every prescribed NON-block segment.

    The prescription itself puts work-intensity riding outside the blocks — a
    tempo section after the main set, an over/under's hot off-legs, a lead-in
    effort. Runs delivered there are the rider following the plan, and nothing
    about them may count against an alignment or a verdict.
    """
    rep_starts = {r["start_s"] for r in reps}
    out = []
    for seg in (planned_segments or []):
        d = seg.get("dur_s") or 0
        if not d or seg.get("start_s") in rep_starts:
            continue
        lo, hi = seg.get("lo"), seg.get("hi")
        if lo is None or hi is None:
            continue
        lo, hi = float(lo), float(hi)
        out.append((float(seg["start_s"]), float(seg["start_s"]) + d,
                    min(lo, hi), max(lo, hi)))
    return out


def _band_tol(frac: float) -> float:
    return max(0.05, 0.08 * frac)
