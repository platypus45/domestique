"""Truthful workout-title generator (Wave 2A).

A pure function that re-derives a workout's filename + display name from its
trusted content class and its parsed ``.zwo`` timing blocks, reproducing the
existing library grammar ``{class}_{structure}_{total}min`` so the title tells
the truth about the body.

Design (per the Wave-2A contract / the three Wave-0 grills):

* **Gate A — multi-block rep sum.** Interval counts come from
  :func:`classify_library_content._detect_interval_signature`, which now sums
  identical interval shapes across recovery-separated ``IntervalsT`` blocks
  (``anaerobic_1min_15x_72min`` = three ``Repeat="5"`` blocks = 15x, not 5x).

* **Gate B — descriptor words.** Non-uniform structures emit an EXISTING
  grammar word rather than a fake ``NxMmin``: ``ramp`` (a warmup/ramp dominates
  the work), ``pyramid`` (monotonic rung structure that turns around),
  ``progression`` (monotonically rising steady blocks), ``mixed`` (≥2 distinct
  hard interval shapes), ``steady`` (a single sustained block / band-only).

* **Gate C — confidence-gated class token.** ``confidence >= 0.6`` bakes the
  canonical multi-token ``primary`` into the title (``over_under``,
  ``sweet_spot``, ``vo2_short`` kept intact — never ``split('_')[0]``).
  ``confidence < 0.6`` falls back to a SOFT token: the dominant-zone band
  (``z2`` / ``z3`` / …) or a generic descriptor — never a precise pattern claim
  (``over_under`` / ``vo2_short`` / ``anaerobic``) the classifier isn't
  confident about.

The function is deterministic and never touches disk beyond parsing the given
``.zwo`` (read-only). It returns ``(filename, display_name)``; the caller owns
collision suffixes and the actual rename.
"""

from __future__ import annotations

from pathlib import Path

import classify_library_content as clc

# Confidence at/above which the canonical class token is trustworthy enough to
# bake into a user-facing title (Gate C). Below this the title uses a soft,
# non-committal token. Anchored to the G1 ground-truth grill: every classifier
# error found sat at conf <= 0.6; the <0.6 tail is genuinely ambiguous.
CONF_GATE = 0.6

# Soft, non-committal class token per dominant zone band. Used when confidence
# is below CONF_GATE so the title never overstates a precise pattern. These are
# deliberately generic intensity words, NOT the precise pattern classes.
_SOFT_BAND_TOKEN = {
    "z1": "recovery",
    "z2": "endurance",
    "z3": "tempo",
    "z4": "threshold",
    "z5": "vo2",
    "z6": "anaerobic",
    "z7": "neuromuscular",
}

# Pretty display label per canonical class token (mirrors the classifier's own
# table, extended with the soft tokens so a soft-token title still reads well).
_DISPLAY_LABEL = dict(clc._CLASS_LABEL_V104)
_DISPLAY_LABEL.update({
    "vo2": "VO2",
    "free_ride": "Free Ride",
})

# Pretty descriptor label per structure word (Gate B). ``ou`` is the existing
# library word for an over-under alternation that has no clean rep count.
_STRUCT_LABEL = {
    "ramp": "Ramp",
    "pyramid": "Pyramid",
    "progression": "Progression",
    "mixed": "Mixed",
    "steady": "Steady",
    "ou": "Over/Under",
}


def _fmt_dur(secs: int) -> str:
    """Grammar duration token: whole minutes as ``Nmin`` else ``Ns``."""
    if secs >= 60 and secs % 60 == 0:
        return f"{secs // 60}min"
    return f"{secs}s"


def _interval_structure_token(sig: tuple[int, int, int, float]) -> str:
    """``{N}x{ON}{unit}`` — the count-first uniform-interval skeleton (1,330
    files on disk use this form, e.g. ``15x1min``, ``8x10s``)."""
    reps, on_s, _off_s, _on_p = sig
    return f"{reps}x{_fmt_dur(on_s)}"


def _is_ramp(segments: list[dict]) -> bool:
    """True when the work is a single continuous ramp — either a literal
    ``<Ramp>`` element that dominates the work, OR an equal-duration staircase
    of ``SteadyState`` blocks that climbs monotonically across a wide power
    range (the ramp-test shape, e.g. 150%→600% FTP in 15s steps). These are the
    ``_ramp_`` files. A gentler few-block rise is handled by ``_is_progression``
    and stays a progression."""
    work = [s for s in segments
            if s["kind"] not in ("warmup", "cooldown", "free_ride")]
    if not work:
        return False
    has_intervals = any(s["kind"] == "intervals" for s in work)
    if has_intervals:
        return False

    # 1. Literal <Ramp> element dominating the work.
    ramp_s = sum(s.get("duration_s", 0) for s in work if s["kind"] == "ramp")
    work_s = sum(s.get("duration_s", 0) for s in work)
    if work_s > 0 and ramp_s / work_s >= 0.60:
        return True

    # 2. Equal-duration staircase sweeping a wide power range monotonically.
    steady = [s for s in work if s["kind"] == "steady"]
    if len(steady) >= 5:
        powers = [s["power"] for s in steady]
        rising = all(b >= a - 1e-9 for a, b in zip(powers, powers[1:]))
        wide = (max(powers) - min(powers)) >= 0.50
        durs = {s.get("duration_s", 0) for s in steady}
        equal_step = len(durs) == 1
        if rising and wide and equal_step:
            return True
    return False


def _hard_steady_blocks(segments: list[dict]) -> list[dict]:
    """Sustained steady work blocks (≥60 s, ≥76% FTP = Z3+). These are the
    'rungs' used to tell progression / pyramid / mixed apart."""
    return [s for s in segments
            if s["kind"] == "steady"
            and s.get("duration_s", 0) >= 60
            and s.get("power", 0.0) >= 0.76]


def _is_progression(blocks: list[dict]) -> bool:
    """≥3 sustained blocks whose power rises monotonically (each ≥+0.03 FTP over
    the last) and never turns back down — the ``_progression_`` shape."""
    if len(blocks) < 3:
        return False
    powers = [b["power"] for b in blocks]
    rises = 0
    for a, b in zip(powers, powers[1:]):
        if b < a - 1e-9:
            return False  # any drop disqualifies a pure progression
        if b >= a + 0.03:
            rises += 1
    return rises >= 2


def _structure_token(primary: str, features: dict, segments: list[dict],
                     sig: tuple[int, int, int, float] | None) -> str:
    """Pick the structure token (Gate A + Gate B).

    Priority:
      1. ladder / pyramid  → ``pyramid`` (rung structure that the classifier
         already flagged via ``is_ladder``)
      2. uniform interval  → ``{N}x{ON}{unit}`` (Gate A summed reps; only from
         real IntervalsT blocks)
      2b. over_under w/o clean IntervalsT shape → ``ou``
      3. warmup-ramp work  → ``ramp``
      4. ≥2 distinct hard interval shapes → ``mixed``
      5. rising steady set  → ``progression``
      6. otherwise          → ``steady`` (single sustained / band-only)
    """
    # 1. Ladder / pyramid — the classifier's rung detector already fired.
    if features.get("is_ladder", False):
        return "pyramid"

    # 2. Uniform interval set — a clean repeated shape from REAL IntervalsT
    #    blocks. We only trust a ``{N}x`` token when it comes from explicit
    #    IntervalsT elements; the steady-pair fallback in
    #    ``_detect_interval_signature`` is unreliable for bodies built entirely
    #    from SteadyState pairs (it latches onto a minor sub-pattern), so those
    #    are routed to a descriptor word below instead of a false rep count.
    iv_shapes = {
        (s.get("on_s", 0), round(s.get("on_power", 0.0), 2))
        for s in segments if s["kind"] == "intervals"
    }
    if iv_shapes and sig is not None and len(iv_shapes) == 1:
        return _interval_structure_token(sig)

    # 2b. Over-unders with no clean IntervalsT shape → the existing ``ou``
    #     descriptor (244 of 379 over_under-named files are SteadyState-pair
    #     alternations the rep-count grammar can't honestly summarise).
    if primary == "over_under":
        return "ou"

    # 3. The work is a single ramp sweep (checked before progression: a wide
    #    monotonic staircase is a ramp; a gentle few-block rise is a
    #    progression).
    if _is_ramp(segments):
        return "ramp"

    # 5 (early-out for >1 hard interval shape, but only if no clean dominant
    #     sig was producible). ≥2 distinct interval shapes = a mixed set.
    if len(iv_shapes) >= 2:
        return "mixed"

    # 4 / 6. Steady-block structure: progression vs mixed vs single steady.
    blocks = _hard_steady_blocks(segments)
    if _is_progression(blocks):
        return "progression"
    if len(blocks) >= 2:
        # Multiple distinct hard steady efforts that neither rise monotonically
        # nor form a ladder → a mixed set.
        distinct = {round(b["power"], 2) for b in blocks}
        if len(distinct) >= 2:
            return "mixed"
    # Single sustained block (or band-only Z2/recovery) — steady.
    return "steady"


def _class_token(primary: str | None, confidence: float,
                 features: dict) -> str:
    """Gate C — the confidence-gated class token.

    conf >= CONF_GATE → canonical multi-token ``primary`` (kept intact).
    conf <  CONF_GATE → SOFT token from the dominant zone band, never a precise
    pattern claim. ``primary is None`` (free_ride / no power) → ``free_ride``.
    """
    if primary is None:
        return "free_ride"
    if confidence >= CONF_GATE:
        return primary
    # Soft token: lean on the dominant band the classifier measured, falling
    # back to the peak band, then a neutral 'endurance'. Crucially this is the
    # GENERIC intensity word, never over_under / vo2_short / anaerobic.
    band = (features.get("dominant_segment_band")
            or features.get("peak_band")
            or "z2")
    return _SOFT_BAND_TOKEN.get(band, "endurance")


def _display_name(class_token: str, struct_token: str,
                  total_min: int, features: dict,
                  sig: tuple[int, int, int, float] | None) -> str:
    """Human-readable Layer-3 string, consistent with the filename's claims."""
    label = _DISPLAY_LABEL.get(class_token,
                               class_token.replace("_", " ").title())

    # Interval structure → spell out reps × on/off @ peak.
    iv_token = struct_token[0].isdigit() and "x" in struct_token
    if iv_token and sig is not None:
        reps, on_s, off_s, on_p = sig
        peak_pct = int(round(on_p * 100))
        body = f"{reps}×{_fmt_dur(on_s)}/{_fmt_dur(off_s)} @ {peak_pct}%"
        return f"{label} {total_min}min — {body}"

    # Descriptor structure → name the shape.
    if struct_token in _STRUCT_LABEL:
        if struct_token == "steady":
            band = (features.get("dominant_segment_band")
                    or features.get("peak_band") or "z2")
            return f"{label} {total_min}min — {band.upper()}"
        return f"{label} {total_min}min — {_STRUCT_LABEL[struct_token]}"

    return f"{label} {total_min}min"


def title_from_zwo(zwo_path: str | Path,
                   class_entry: dict) -> tuple[str, str]:
    """Re-derive ``(filename, display_name)`` for a workout from its trusted
    class + parsed ``.zwo`` blocks. Pure / deterministic / read-only.

    Parameters
    ----------
    zwo_path:
        Path to the ``.zwo`` file (parsed for its timing blocks only).
    class_entry:
        The file's entry from ``.content_classification.json`` — needs at least
        ``primary``, ``confidence`` and ``features`` (``duration_s``,
        ``dominant_segment_band`` / ``peak_band``). ``is_ladder`` is read from
        ``features`` when present.

    Returns
    -------
    (filename, display_name)
        ``filename`` reproduces ``{class}_{structure}_{total}min.zwo``;
        ``display_name`` is the matching human string. The caller owns collision
        suffixes — this function does not look at the rest of the library.
    """
    zwo_path = Path(zwo_path)
    primary = class_entry.get("primary")
    confidence = float(class_entry.get("confidence", 0.0) or 0.0)
    features = dict(class_entry.get("features") or {})

    # Always parse the body so structure is derived from ground truth, not from
    # whatever the stored display string claimed.
    _power, _tags, _meta, segments = clc.parse_zwo_full(zwo_path)

    # duration: prefer the parsed length, fall back to the stored feature.
    total_s = len(_power) or int(features.get("duration_s", 0) or 0)
    total_min = clc._round_minutes(total_s)

    class_token = _class_token(primary, confidence, features)

    # Free-ride / no-power workouts have no structure to encode.
    if primary is None and not segments:
        fname = f"free_ride_{total_min}min.zwo"
        return fname, f"Free Ride {total_min}min"

    sig = clc._detect_interval_signature(segments)
    struct_token = _structure_token(primary or "", features, segments, sig)

    filename = f"{class_token}_{struct_token}_{total_min}min.zwo"
    display_name = _display_name(class_token, struct_token, total_min,
                                 features, sig)
    return filename, display_name
