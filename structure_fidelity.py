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
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

__all__ = [
    "parse_zwo_text", "parse_zwo_file", "score_structure",
    "WORK_FLOOR_FRAC", "TOL_FRAC", "TOL_MIN_W", "TRANSIENT_GRACE_S",
    "ALIGN_MAX_OFFSET_S", "MISSING_BELOW_FLOOR_FRAC", "MISSING_TARGET_FRAC",
]

# ── Locked fidelity constants (documented in the module docstring) ──────────
WORK_FLOOR_FRAC = 0.75          # midpoint >= this ⇒ segment is a graded rep
TOL_FRAC = 0.05                 # on-target band: ±5 % of the second's target…
TOL_MIN_W = 10.0                # …or ±10 W, whichever is larger
TRANSIENT_GRACE_S = 3           # ERG step-response seconds excluded per rep
ALIGN_MAX_OFFSET_S = 120        # global alignment search, ±s at 1 s steps
MISSING_BELOW_FLOOR_FRAC = 0.5  # absent/below-floor > this ⇒ rep missing
MISSING_TARGET_FRAC = 0.90      # floor also capped at 90 % of the rep target


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
