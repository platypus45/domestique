"""Serving a .zwo: the capacity cap, the outdoor wrapper, the response.

Five helpers shared by the six download endpoints and by both FIT builders.
They are here rather than in the endpoints because `_cap_zwo_bytes` is the one
place the capacity ceiling is applied -- the .zwo the athlete downloads and the
.fit pushed to the head unit must be capped identically or the two disagree
about the workout.

Unlike the other extracted modules this one imports fastapi, because building
the response is what it does.
"""
from fastapi.responses import FileResponse, Response


def _capacity_cap_active(pm, force: bool = False) -> bool:
    """task #24: True when the measured-capacity short-rep cap should apply to a
    served ZWO/FIT for this profile. Requires a trustworthy MEASURED Pmax
    (pmax_is_set), power target_mode (hr prescriptions are untouched --
    target_mode wins), and the toggle == "on" (or ``force`` for a PROMPT
    APPROVE with ?cap=1). "prompt"/"off" do NOT auto-apply on their own."""
    try:
        if not pm.pmax_is_set:
            return False
        if pm.target_mode == "hr":
            return False
        if force:
            return True
        return pm.cap_short_intervals == "on"
    except Exception:
        return False

def _cap_zwo_bytes(raw: bytes, filename: str, pm) -> bytes:
    """Apply the measured-capacity cap to ZWO bytes, or return them UNCHANGED.

    Gate is the CALLER's responsibility via ``_capacity_cap_active`` -- this
    only does the transform once the caller decided to. Ramp-test exemption is
    handled inside ``cap_zwo_text`` via the ``ftp_test_ramp*`` filename guard
    (the grill-validated ramp marker -- the content classifier has no distinct
    "ramp" primary to add). On any decode hiccup the original bytes are
    returned (never corrupt a download)."""
    import capacity_cap as _cc
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    txt2, n, _ = _cc.cap_zwo_text(
        txt, float(pm.ftp), float(pm.cp), float(pm.pmax_w),
        filename=filename)
    if n == 0:
        return raw  # byte-identical no-op
    return txt2.encode("utf-8")

def _wrap_zwo_outdoor(xml_text: str, transit_min: int, spin_min: int) -> str:
    """G1 (v2.1) — frame a prescribed indoor block inside a real OUTDOOR ride:
    prepend a flat transit warmup and append a spin-home cooldown. The prescribed
    body is passed through UNCHANGED (string-level insert, not re-serialized, so
    the middle is byte-identical). Export-only: this is the download path, so it
    never touches the planner's accounted/weekly TSS — the transit + spin-home are
    OFF-PLAN additional easy minutes by construction."""
    t_s = max(0, int(transit_min)) * 60
    s_s = max(0, int(spin_min)) * 60
    out = xml_text
    if t_s and "<workout>" in out:
        out = out.replace(
            "<workout>",
            f'<workout>\n        <Warmup Duration="{t_s}" PowerLow="0.40" '
            f'PowerHigh="0.60"/>  <!-- G1 transit to climb (off-plan) -->', 1)
    if s_s and "</workout>" in out:
        out = out.replace(
            "</workout>",
            f'        <Cooldown Duration="{s_s}" PowerLow="0.50" PowerHigh="0.40"/>'
            f'  <!-- G1 spin home (off-plan) -->\n    </workout>', 1)
    return out

def _zwo_download_response(path, filename: str, outdoor: int,
                          transit_min: int, spin_min: int,
                          cap_active: bool = False):
    """Shared ZWO download: plain FileResponse, or (G1) an outdoor-wrapped copy.

    task #24: when ``cap_active`` the served body is first capped to the
    rider's measured-power envelope (short reps only). A no-op cap (nothing
    qualifies) still returns the byte-identical FileResponse, so the OFF /
    non-qualifying paths are unchanged."""
    plain = FileResponse(
        path, filename=filename, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    capped_bytes = None
    if cap_active:
        from profile_manager import ProfileManager
        try:
            raw = path.read_bytes()
            capped = _cap_zwo_bytes(raw, filename, ProfileManager.get())
            capped_bytes = capped if capped is not raw and capped != raw else None
        except Exception:
            capped_bytes = None
    if not outdoor:
        if capped_bytes is None:
            return plain  # byte-identical to the file on disk
        return Response(
            capped_bytes, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    try:
        if capped_bytes is not None:
            body_text = capped_bytes.decode("utf-8")
        else:
            body_text = path.read_text(encoding="utf-8")
        wrapped = _wrap_zwo_outdoor(body_text, transit_min, spin_min)
    except Exception:
        return plain if capped_bytes is None else Response(
            capped_bytes, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    out_name = filename.rsplit(".", 1)[0] + "_outdoor.zwo"
    return Response(
        wrapped, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'})

def _cap_active_for_download(cap: int) -> bool:
    """task #24: resolve the ZWO/FIT serve cap for the active profile.
    ``cap=1`` (PROMPT APPROVE) forces the cap; otherwise the "on" toggle. Both
    still require pmax_is_set + power mode (``_capacity_cap_active``)."""
    try:
        from profile_manager import ProfileManager
        return _capacity_cap_active(ProfileManager.get(), force=bool(cap))
    except Exception:
        return False
