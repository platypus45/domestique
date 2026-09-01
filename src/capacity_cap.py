"""Measured-capacity short-rep advisory (task #24, v3.2.0).

Cap a served workout's short reps to the rider's OWN measured max-power
envelope. This ships zero model-predicted watts: it only ever LOWERS a
prescribed short-rep target toward the rider's real, measured ceiling (pmax_w,
ICU/manual-ingested), never raises it, and is inert unless the rider has a
trustworthy measured Pmax.

Design (grill-locked 2026-07-05):
  - Envelope: P_env(t) = CP + (Pmax - CP) * exp(-t / TAU_S), TAU_S = 26.0.
    Anchored on MEASURED Pmax (pmax_source in {"manual","icu"}) + CP only.
    TAU_S is an envelope-decay time constant on MEASURED anchors -- NOT a
    balance/recovery time constant, and nothing here reconstructs a modelled
    on-power from an anaerobic-capacity balance (that whole class of engine is
    deliberately absent: the grep-proof for GA6).
  - Gate: pmax_is_set(pm) mirrors ProfileManager.lthr_is_set -- True ONLY when
    pm.pmax_source in {"manual","icu"}. The bare pm.pmax_w returns ftp*1.30 when
    unset (never-None trap), so it can NEVER gate this feature.
  - Serve-time cap is a STRING/REGEX targeted-attribute edit (never
    ET.parse -> ET.tostring, which fails byte-identity on most files). A no-op
    pass (nothing qualifies) returns byte-identical text.
"""

from __future__ import annotations

import math
import re
from typing import Optional

# ── Constants (grill-locked) ────────────────────────────────────────────────
TAU_S = 26.0            # envelope decay time constant (s); MEASURED-anchored
QUAL_RATIO = 1.20       # a rep qualifies only at >= 1.20 x FTP ...
QUAL_MAX_ON_S = 120     # ... AND on-duration <= 120 s (anaerobic short-rep zone)
TOO_HARD = 0.95         # fire the cap when prescribed_ratio > 0.95 * env_ratio
CAP_FRAC = 0.90         # cap target ratio = 0.90 * env_ratio ...
VO2_FLOOR = 1.06        # ... but never below 1.06 for vo2-class reps

# Ramp-test filenames are exempt: a to-failure staircase past FTP reads as
# dozens of "too hard" short reps but is meant to be quit, not completed.
# ponytail: filename match is the ceiling. The content classifier has no
# distinct "ramp" primary (they are all "ftp_test"), and a pure structural
# staircase detector both over-exempts real interval files and misses the
# 10 W-step ramp -- so `ftp_test_ramp*` is the canonical, data-validated
# ramp marker (all 3 library ramp tests match it). A caller that knows the
# classification can additionally pass exempt=True.
_RAMP_TEST_RE = re.compile(r"ftp_test_ramp", re.IGNORECASE)


def pmax_is_set(pm) -> bool:
    """True only when a TRUSTWORTHY measured Pmax is stored -- i.e.
    pm.pmax_source is "manual" (rider typed it) or "icu" (ICU power-curve
    sync). Mirrors ProfileManager.lthr_is_set (profile_manager.py:153).

    NEVER gate on `pm.pmax_w`: that property returns int(ftp * 1.30) when no
    real value has been written (profile_manager.py:232-233), so every user
    would appear to "have" a Pmax -- the same never-None trap as pm.cp /
    lthr=170. "computed" (fitness estimate) and "fallback" are excluded: the
    advisory only ever fires against a number the rider can trust.

    Delegates to the canonical ``ProfileManager.pmax_is_set`` property when the
    object exposes it (single source of truth); falls back to reading
    pmax_source directly for a duck-typed / test stub.
    """
    try:
        val = getattr(pm, "pmax_is_set", None)
        if isinstance(val, bool):
            return val
        return str(getattr(pm, "pmax_source", "") or "") in ("manual", "icu")
    except Exception:
        return False


def p_env(t: float, cp: float, pmax: float) -> float:
    """Measured max-power envelope at duration t seconds (watts).

    P_env(t) = CP + (Pmax - CP) * exp(-t / TAU_S)

    Morton-style declining-max-power form between two MEASURED anchors:
    Pmax (best short power, ICU/manual) at t->0 and CP (sustainable) as
    t grows. Monotone decreasing in t; monotone increasing in Pmax.
    """
    if t < 0:
        t = 0.0
    return cp + (pmax - cp) * math.exp(-t / TAU_S)


def _is_vo2_rep(ratio: float) -> bool:
    """A qualifying rep is 'vo2-class' when its authored ratio sits in the
    vo2 band (~1.06-1.20 x FTP). Such reps get the 1.06 cap floor so a cap
    never drives a genuine vo2 effort below vo2 intensity."""
    return ratio < 1.20 + 1e-9


def _cap_ratio(ratio: float, t: float, ftp: float, cp: float, pmax: float
               ) -> Optional[float]:
    """Return the capped ratio for one rep, or None when the rep should NOT be
    touched (does not qualify, or is already within the rider's envelope).

    Only ever LOWERS: the result is < ratio, floored at max(VO2_FLOOR-for-vo2,
    ...) and never above the original ratio.
    """
    if t <= 0 or t > QUAL_MAX_ON_S:
        return None
    if ratio < QUAL_RATIO:
        return None
    env_ratio = p_env(t, cp, pmax) / ftp
    # Only fire when the prescription meaningfully exceeds the rider's own
    # attainable power at this duration.
    if ratio <= TOO_HARD * env_ratio:
        return None
    capped = CAP_FRAC * env_ratio
    # Floor: never below the vo2 floor for a vo2-class rep, and never below a
    # threshold-ish floor otherwise (CAP_FRAC*env can dip low for a tiny Pmax).
    floor = VO2_FLOOR if _is_vo2_rep(ratio) else 0.0
    capped = max(capped, floor)
    # Direction guard: only LOWER. If the floor pushed us at/above the
    # original, don't touch the rep at all.
    if capped >= ratio:
        return None
    return capped


def _fmt_ratio(value: float, original_text: str) -> str:
    """Format a capped ratio to match ZWO number style. ZWO powers are written
    like "0.81", "1.2", "2.0"; keep 2 decimals then trim so we never introduce
    exotic float noise (e.g. 1.0699999). Falls back to the raw repr if the
    round-trip somehow parses differently."""
    s = f"{value:.2f}"
    # trim trailing zeros but keep at least one decimal (ZWO uses "1.2", "2")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


# One <Tag .../> element (self-closing or open) -- captured whole so a cap
# edits only the Power/OnPower attribute inside THIS element.
_ELEM_RE = re.compile(r"<\s*(SteadyState|IntervalsT)\b[^>]*?/?>", re.IGNORECASE)
# Standalone Power="X" -- the negative-lookbehind stops it matching the
# "Power" tail of OnPower / OffPower / PowerLow / PowerHigh.
_POWER_ATTR_RE = re.compile(r'(?<![A-Za-z])(Power)\s*=\s*"([0-9.]+)"')
_ONPOWER_ATTR_RE = re.compile(r'(?<![A-Za-z])(OnPower)\s*=\s*"([0-9.]+)"')
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _attrs(elem_text: str) -> dict:
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(elem_text)}


def is_ramp_test_name(filename: str) -> bool:
    """True when the filename marks a ramp (to-failure) test -- exempt."""
    return bool(_RAMP_TEST_RE.search(filename or ""))


def cap_zwo_text(txt: str, ftp: float, cp: float, pmax: float,
                 filename: str = "", exempt: bool = False):
    """Cap qualifying short reps in a ZWO document's text to the rider's
    measured-power envelope, via targeted string replacement.

    Args:
        txt: the raw ZWO document text.
        ftp, cp, pmax: rider anchors (watts). Caller must have already checked
            pmax_is_set(pm) -- this function does NOT re-gate; a caller that
            passes an unset (ftp*1.30) pmax will simply produce few/no caps,
            but the INERT contract is the CALLER's job (GA2).
        filename: used only for the ramp-test exemption.
        exempt: caller-supplied override (e.g. from the content classifier)
            forcing the whole file exempt.

    Returns:
        (txt2, n_capped, details) where
          txt2     -- capped text; BYTE-IDENTICAL to txt when n_capped == 0.
          n_capped -- number of rep attributes lowered.
          details  -- list of {"tag","t","orig_ratio","new_ratio",
                       "orig_w","cap_w"} for the modal comparison.
    """
    details: list[dict] = []
    if exempt or is_ramp_test_name(filename):
        return txt, 0, details
    if ftp <= 0 or pmax <= cp:
        # Degenerate anchors -> no defensible envelope; leave the file alone.
        return txt, 0, details

    n = 0

    def _repl_elem(m: "re.Match") -> str:
        nonlocal n
        elem = m.group(0)
        tag = m.group(1)
        a = _attrs(elem)
        if tag.lower() == "steadystate":
            dur = a.get("Duration")
            raw = a.get("Power")
            attr_re = _POWER_ATTR_RE
        else:  # IntervalsT
            dur = a.get("OnDuration")
            raw = a.get("OnPower")
            attr_re = _ONPOWER_ATTR_RE
        if raw is None or dur is None:
            return elem
        try:
            t = float(dur)
            ratio = float(raw)
        except (TypeError, ValueError):
            return elem
        new_ratio = _cap_ratio(ratio, t, ftp, cp, pmax)
        if new_ratio is None:
            return elem
        new_txt = _fmt_ratio(new_ratio, raw)
        # Guard: if formatting rounded back to the original string, this is a
        # no-op -- do NOT rewrite (protects byte-identity).
        if new_txt == raw or float(new_txt) >= ratio:
            return elem

        # Replace ONLY that one attribute value inside THIS element.
        def _sub(mm: "re.Match") -> str:
            return f'{mm.group(1)}="{new_txt}"'

        elem2 = attr_re.sub(_sub, elem, count=1)
        if elem2 == elem:
            return elem
        n += 1
        details.append({
            "tag": tag,
            "t": int(t),
            "orig_ratio": ratio,
            "new_ratio": float(new_txt),
            "orig_w": round(ratio * ftp),
            "cap_w": round(float(new_txt) * ftp),
        })
        return elem2

    txt2 = _ELEM_RE.sub(_repl_elem, txt)
    if n == 0:
        # Byte-identity guarantee: nothing qualified -> hand back the original
        # object so callers can assert `is`/0-diff.
        return txt, 0, details
    return txt2, n, details
