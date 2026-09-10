"""Heart-rate resolution shared by the FIT builders, the settings surface and
the workout matcher.

These three sat inside the FIT WORKOUT EXPORT section, which made them look
like an export detail. They are not: `_prescription_hr_rows` is the single
resolver every HR number in the app routes through, and `_hr_bias` is the one
chokepoint the rematch and redraw paths consult. Six sections outside FIT
export call them, so leaving them there would have forced the workout-library
extraction to import the export section.

Nothing is new; the definitions are what app.py had.
"""


def _fit_hr_mode() -> bool:
    """True when the athlete's target_mode is 'hr' (IP_HR_ONLY). Kept tiny so
    both FIT builders share one gate; ProfileManager.target_mode already
    degrades to 'power' if the lthr/max_hr invariant is broken."""
    try:
        from profile_manager import ProfileManager
        return ProfileManager.get().target_mode == "hr"
    except Exception:
        return False


def _prescription_hr_rows(pm) -> dict | None:
    """The athlete's custom HR prescription rows (W1) or None for Coggan
    defaults. SINGLE resolver — converter, FIT, hr_axis, /api/settings
    hr_rows and session chips all route through here so the numbers can
    never diverge. Shape: {"z1_high": int, "z2": [lo,hi], "z3": [lo,hi],
    "z4": [lo,hi]} (absolute bpm; validated at the settings write)."""
    rows = pm._athlete.get("hr_prescription_rows_custom")
    return rows if isinstance(rows, dict) else None


def _hr_bias() -> bool:
    """hr target_mode -> soft matcher preference for HR-guidable files
    (v2.5.0 W5). One chokepoint so every rematch/redraw path agrees."""
    return _fit_hr_mode()


def _fit_hr_params() -> tuple[int, int, dict | None]:
    # int() — save_athlete's validator stores lthr as float (e.g. 167.5 from an
    # ICU estimate); the detail endpoint ints too, so chart and FIT round the
    # same base and can't skew by 1-2 bpm (red-team F5).
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    return int(pm.lthr), int(pm.max_hr), _prescription_hr_rows(pm)
