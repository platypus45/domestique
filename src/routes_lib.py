"""The virtual-route library: load, index, filter, score, and profile.

Nineteen helpers that between them own everything the app knows about routes --
routes.json and its index, surface_types.json, the climb/flat predicates, the
suggest scorer, the elevation profiles and the climb .zwo builder.

They lived inside the VIRTUAL ROUTES API section, tangled with the endpoints
that call them. Exactly one caller sat outside that section
(`_gradient_to_power_factor`, from the climb-zwo download), which is what made
this the first feature-sized piece that can move.

The endpoints stay in app.py for now: they carry `@app.*` decorators and moving
them needs an APIRouter, which is a separate change with a separate risk.

MUTABLE MODULE STATE lives here now, not in app.py:
`_ROUTES_CACHE` / `_ROUTES_INDEX` / `_ROUTES_MTIME` and
`_SURFACE_TYPES_CACHE` / `_SURFACE_TYPES_MTIME`. app.py does NOT re-export
them -- `from routes_lib import _ROUTES_CACHE` would bind the list object, and
rebinding app's name would leave this module still reading its own. Anything
that needs to reset them (the tests do) must set them on THIS module.
"""
import json
import os
import re
import threading
from pathlib import Path

from cache import cached
from obs import _log
from paths import ROUTE_DATA, ROUTE_PROFILES_DIR, ROUTE_PROFILES_INDEX


def _slugify_route_name(name: str) -> str:
    """Slugify a route name using the same rule as generate_route_profiles.py."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def _route_key_for(world_slug: str, crs_filename: str) -> tuple[str, str]:
    """Derive (slug, route_key) for a route based on its CRS filename.

    Mirrors the logic in generate_route_profiles.py so the key matches what was
    written to profiles_indexed.json and profiles/<world>__<slug>.json.
    """
    stem = crs_filename[:-4] if crs_filename.lower().endswith(".crs") else crs_filename
    # Virtual-world routes embed "<world>__<slug>" in the filename; real-world
    # routes slugify the whole filename.
    slug = stem.split("__", 1)[1] if "__" in stem else _slugify_route_name(stem)
    return slug, f"{world_slug}/{slug}"

_ROUTES_CACHE: list[dict] = []
_ROUTES_INDEX: dict = {}
_ROUTES_MTIME: float = 0.0
_ROUTES_LOCK = threading.Lock()

# ─── Canonical surface-segment mapping (MASTER_DECISIONS §1) ───────────────
# surface_types.json is authored in lowercase today, but training_live loads
# it as UPPERCASE TACX RoadSurface tokens (_SURFACE_NAME_MAP). Anything that
# crosses a tier (HTTP / WS) MUST downshift to the canonical lowercase enum:
# asphalt | gravel | cobble | dirt | sand | unknown. Emit "unknown" for any
# value outside that enum — never drop.
_SURFACE_CANONICAL_MAP: dict[str, str] = {
    # Already-lowercase canonical forms (pass through).
    "asphalt": "asphalt",
    "gravel": "gravel",
    "cobble": "cobble",
    "dirt": "dirt",
    "sand": "sand",
    "unknown": "unknown",
    # UPPERCASE TACX tokens (from training_live._SURFACE_NAME_MAP leak paths).
    "ASPHALT": "asphalt",
    "PAVED": "asphalt",
    "TARMAC": "asphalt",
    "COBBLESTONES_HARD": "cobble",
    "COBBLESTONES_SOFT": "cobble",
    "BRICK_ROAD": "cobble",
    "CONCRETE_PLATES": "cobble",
    "GRAVEL": "gravel",
    "OFF_ROAD": "gravel",
    "DIRT": "dirt",
    "TRAIL": "dirt",
    "SAND": "sand",
    "WOODEN_BOARDS": "unknown",
    "CATTLE_GRID": "unknown",
    "ICE": "unknown",
}

def _canonical_surface(raw) -> str:
    """Map any surface token (lower/upper) to the canonical lowercase enum.
    Unknown inputs fall through to "unknown" rather than silently dropping."""
    if not raw:
        return "unknown"
    s = str(raw).strip()
    return _SURFACE_CANONICAL_MAP.get(s) or _SURFACE_CANONICAL_MAP.get(s.upper()) or _SURFACE_CANONICAL_MAP.get(s.lower(), "unknown")

_SURFACE_TYPES_CACHE: dict | None = None
_SURFACE_TYPES_MTIME: float = 0.0
_SURFACE_TYPES_LOCK = threading.Lock()

def _load_surface_types_db() -> dict:
    """Return a cached copy of surface_types.json, reloaded on mtime change.

    Shape: `{"<region>/<slug>": [{start_km, end_km, surface}, ...]}`. Returns
    an empty dict if the file is missing or malformed — callers fall through
    to a single "unknown" segment so the frontend always has renderable data.
    """
    global _SURFACE_TYPES_CACHE, _SURFACE_TYPES_MTIME
    path = Path(__file__).parent / "surface_types.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _SURFACE_TYPES_LOCK:
        if _SURFACE_TYPES_CACHE is None or mtime != _SURFACE_TYPES_MTIME:
            try:
                _SURFACE_TYPES_CACHE = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                _log.error(f"Failed to load surface_types.json: {e}")
                _SURFACE_TYPES_CACHE = {}
            _SURFACE_TYPES_MTIME = mtime
        return _SURFACE_TYPES_CACHE or {}

def _route_surface_segments(route_id: str, distance_km: float | None = None,
                             lap_info: dict | None = None) -> list[dict]:
    """Canonical `surface_segments` for a given route id.

    Returns a list of `{start_km, end_km, surface}` dicts. Surface values are
    canonical lowercase (MASTER_DECISIONS §1). When no surface data exists
    for the route, emits a single "unknown" segment covering 0..distance_km
    (or empty list when distance is unknown). Never returns None; the
    frontend always has something to paint.

    v3.6.0-fix29 — when a route carries `lap_route.laps > 1`,
    `surface_types.json` stores only ONE base-lap's worth of segments while
    `distance_km` reflects the fully multiplied distance. Tile the base
    segments `laps` times with `base_km` offsets so lap 2+ does not fall
    through the frontend's gap-filler as implicit asphalt (bug: Cobbled
    Classic Sectors × 2, Hidden Cruise 47 × 2, and any `lap_route.laps>1`).
    """
    if not route_id:
        return []
    db = _load_surface_types_db()
    raw = db.get(route_id) or []
    if not raw:
        if distance_km and distance_km > 0:
            return [{"start_km": 0.0, "end_km": float(distance_km), "surface": "unknown"}]
        return []
    base: list[dict] = []
    for seg in raw:
        try:
            s = float(seg.get("start_km", 0.0) or 0.0)
            e = float(seg.get("end_km", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue
        base.append({
            "start_km": s,
            "end_km": e,
            "surface": _canonical_surface(seg.get("surface")),
        })
    # Multi-lap tiling (fix29). Only fires when the caller passes a
    # `lap_route` dict with `laps > 1`; single-lap routes return base as-is.
    laps = 1
    base_km = 0.0
    if isinstance(lap_info, dict):
        try:
            laps = int(lap_info.get("laps", 1) or 1)
        except (TypeError, ValueError):
            laps = 1
        try:
            base_km = float(lap_info.get("base_km", 0.0) or 0.0)
        except (TypeError, ValueError):
            base_km = 0.0
    if laps <= 1 or base_km <= 0:
        return base
    # Tile stride: prefer the base segments' own max end_km over
    # `lap_route.base_km` — authored data sometimes rounds base_km to 2 dp
    # (e.g. 11.85) while the segment file carries the unrounded value
    # (11.859), which would overlap lap 1 into lap 2 if we blindly used
    # base_km as the offset. The surface data is the canonical base.
    base_end = max(s["end_km"] for s in base) if base else base_km
    stride = base_end if base_end > 0 else base_km
    tiled: list[dict] = []
    for lap_idx in range(laps):
        offset = lap_idx * stride
        for seg in base:
            tiled.append({
                "start_km": seg["start_km"] + offset,
                "end_km": seg["end_km"] + offset,
                "surface": seg["surface"],
            })
    return tiled

def _build_routes_index(routes: list[dict]) -> dict:
    """Build inverted indexes for hot filter fields."""
    by_id: dict[str, dict] = {}
    by_region: dict[str, list[int]] = {}
    by_source: dict[str, list[int]] = {}
    by_category: dict[str, list[int]] = {}
    by_primary_surface: dict[str, list[int]] = {}
    by_terrain: dict[str, list[int]] = {}
    by_finish: dict[str, list[int]] = {}
    for i, r in enumerate(routes):
        rid = r.get("id", "")
        if rid:
            by_id[rid] = r
        by_region.setdefault(r.get("region", ""), []).append(i)
        by_source.setdefault(r.get("source", ""), []).append(i)
        by_category.setdefault(r.get("category", ""), []).append(i)
        by_primary_surface.setdefault(r.get("primary_surface", ""), []).append(i)
        by_terrain.setdefault(r.get("terrain", ""), []).append(i)
        by_finish.setdefault(r.get("finish_type", ""), []).append(i)
    return {
        "by_id": by_id,
        "by_region": by_region,
        "by_source": by_source,
        "by_category": by_category,
        "by_primary_surface": by_primary_surface,
        "by_terrain": by_terrain,
        "by_finish": by_finish,
    }

def _load_routes_v2(force: bool = False) -> tuple[list[dict], dict]:
    """Load routes.json v2 with mtime-based invalidation.

    Returns (routes, index). Caches indefinitely; reloads on file mtime change.
    Thread-safe via _ROUTES_LOCK.
    """
    global _ROUTES_CACHE, _ROUTES_INDEX, _ROUTES_MTIME
    if not ROUTE_DATA.exists():
        return [], {"by_id": {}, "by_region": {}, "by_source": {}, "by_category": {},
                    "by_primary_surface": {}, "by_terrain": {}, "by_finish": {}}
    try:
        mtime = ROUTE_DATA.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _ROUTES_LOCK:
        if force or not _ROUTES_CACHE or mtime != _ROUTES_MTIME:
            try:
                data = json.loads(ROUTE_DATA.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                _log.error(f"Failed to load routes.json: {e}")
                return _ROUTES_CACHE, _ROUTES_INDEX
            # Accept either flat list (v2) or legacy dict shape — normalize.
            if isinstance(data, list):
                _ROUTES_CACHE = data
            else:
                # Legacy shape: {"worlds": [...]}; flatten best-effort so the
                # cache never ends up in a broken intermediate state.
                flat = []
                for w in (data.get("worlds") if isinstance(data, dict) else []) or []:
                    for r in w.get("routes", []):
                        flat.append(r)
                _ROUTES_CACHE = flat
            # Attach canonical surface_segments to every route entry so the
            # list endpoint (/api/routes) and the detail endpoint (/api/routes/{id})
            # both return the spatial data the mini-map needs. Single point of
            # truth — delegates to `_route_surface_segments` so malformed
            # `surface_types.json` entries cannot 500 the whole /api/routes
            # response (QA-CODE #2: the inline float() parse was unguarded).
            for _r in _ROUTES_CACHE:
                rid = _r.get("id")
                if not rid:
                    continue
                _r["surface_segments"] = _route_surface_segments(
                    rid, _r.get("distance_km"), _r.get("lap_route")
                )
            _ROUTES_INDEX = _build_routes_index(_ROUTES_CACHE)
            _ROUTES_MTIME = mtime
        return _ROUTES_CACHE, _ROUTES_INDEX

# Shared climb/flat predicates so the empty-state hint counter and the main
# pipeline agree on what "climb required" / "no climbs" mean. Previously these
# were inline and drifted by ~60 routes. Changes must land here, not in two
# places.
_CLIMB_CATEGORIES = {"cat1", "cat2", "cat3", "cat4", "hc"}

def _is_climb_route(r: dict) -> bool:
    """True if the route has real climbing content (not just a single kicker).

    Canonical predicate used by both the main pipeline and the empty-state
    "would_match" counter. Categories are compared case-insensitively so
    upstream generators emitting "HC" vs "hc" both register as climbs.
    """
    cat = (r.get("category") or "").lower()
    if cat in _CLIMB_CATEGORIES:
        return True
    if (r.get("climb_count") or 0) >= 1 and (r.get("max_grade") or 0) >= 4.0:
        return True
    if r.get("terrain") == "climb":
        return True
    return False

def _is_flat_route(r: dict) -> bool:
    """True if the route is flat enough for a "no climbs today" request.
    Loosened from max_grade<5 to max_grade<8 so that flat gravel/cobble
    sportives (which carry short kickers ≥5%) can still satisfy a flat query."""
    if r.get("terrain") == "climb":
        return False
    if (r.get("max_grade") or 0) >= 8.0:
        return False
    return True

def _route_summary(r: dict) -> dict:
    """Project a route entry to the lightweight list shape (no crs_path)."""
    # Prefer the stored value when present — the prior version unconditionally
    # recomputed at 23 km/h (Z2 average), which overrode any hand-tuned
    # duration baked into routes.json. Only fall back to the flat-speed
    # estimator when the stored field is missing/invalid.
    stored = r.get("est_duration_min_z2")
    km = r.get("distance_km") or 0
    if isinstance(stored, (int, float)) and stored and stored > 0:
        est_duration_min_z2 = int(round(stored))
    elif km and km > 0:
        est_duration_min_z2 = int(round(km / 23.0 * 60))
    else:
        est_duration_min_z2 = stored
    # Disk-accurate open locator derived from crs_path (the single source of
    # truth). The logical `region` above is unreliable for opening — 21 routes
    # are tagged netherlands_gravel/gravel/etc. but physically live in
    # courses/gravel_europe/ — so the frontend opens via crs_region + file.
    # crs_path itself stays out of the list shape (kept lightweight).
    _crs = r.get("crs_path") or ""
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "region": r.get("region"),
        "crs_region": (os.path.basename(os.path.dirname(_crs)) if _crs else None),
        "file": os.path.basename(_crs) or None,
        "source": r.get("source"),
        "distance_km": r.get("distance_km"),
        "climb_m": r.get("climb_m"),
        "max_grade": r.get("max_grade"),
        "avg_grade_signed": r.get("avg_grade_signed"),
        "difficulty_score": r.get("difficulty_score"),
        "category": r.get("category"),
        "terrain": r.get("terrain"),
        "finish_type": r.get("finish_type"),
        "primary_surface": r.get("primary_surface"),
        "has_gravel": r.get("has_gravel"),
        "has_cobble": r.get("has_cobble"),
        "loop": r.get("loop"),
        "lap_route": r.get("lap_route"),
        "est_duration_min_z2": est_duration_min_z2,
        "est_tss": r.get("est_tss"),
        "tags": r.get("tags", []),
        "preview_profile": r.get("preview_profile", []),
        "climb_count": r.get("climb_count", 0),
        "surface_mix_pct": r.get("surface_mix_pct") or {},
        "surface_segments": r.get("surface_segments") or [],
        "primary_climb": r.get("primary_climb"),
    }

def _parse_csv_param(val) -> list[str]:
    """Parse a comma-separated query param into a list of lowercase tokens."""
    if not val:
        return []
    if isinstance(val, list):
        raw = ",".join(val)
    else:
        raw = str(val)
    return [t.strip().lower() for t in raw.split(",") if t.strip()]

def _apply_route_filters(
    routes: list[dict],
    *,
    source: str | None = None,
    regions: list[str] | None = None,
    surfaces: list[str] | None = None,
    terrains: list[str] | None = None,
    categories: list[str] | None = None,
    finishes: list[str] | None = None,
    min_km: float | None = None,
    max_km: float | None = None,
    loop_only: bool = False,
    lap_only: bool = False,
    max_difficulty: float | None = None,
    search: str | None = None,
) -> list[dict]:
    """Apply all filter flags; returns the filtered list (order preserved)."""
    # Derive the real-world region set from the loaded routes so new regions
    # (like Flanders) auto-register without needing a hardcoded list update.
    real_world_regions = {r.get("region") for r in routes
                          if r.get("source") == "real_world" and r.get("region")}
    # Accept BOTH the UI-facing token "__real_world__" (returned by
    # /api/routes/regions.real_world_meta.region) and the short "real_world"
    # alias for robustness.
    _REAL_WORLD_TOKENS = {"real_world", "__real_world__"}
    region_set: set[str] | None = None
    if regions:
        expanded: set[str] = set()
        for reg in regions:
            if reg in _REAL_WORLD_TOKENS:
                expanded |= real_world_regions
            else:
                expanded.add(reg)
        region_set = expanded

    surface_set = set(surfaces) if surfaces else None
    terrain_set = set(terrains) if terrains else None
    category_set = set(categories) if categories else None
    finish_set = set(finishes) if finishes else None
    search_lc = search.lower() if search else None

    out = []
    for r in routes:
        if source and source != "any":
            if r.get("source") != source:
                continue
        if region_set is not None and r.get("region") not in region_set:
            continue
        if surface_set is not None:
            ps = (r.get("primary_surface") or "").lower()
            has_g = r.get("has_gravel")
            has_c = r.get("has_cobble")
            # A route matches if its primary_surface is in the filter set OR
            # if it has gravel/cobble sectors for those tokens respectively.
            match = ps in surface_set
            if not match and "gravel" in surface_set and has_g:
                match = True
            if not match and "cobble" in surface_set and has_c:
                match = True
            if not match:
                continue
        if terrain_set is not None and (r.get("terrain") or "").lower() not in terrain_set:
            continue
        if category_set is not None and (r.get("category") or "").lower() not in category_set:
            continue
        if finish_set is not None and (r.get("finish_type") or "").lower() not in finish_set:
            continue
        if min_km is not None and (r.get("distance_km") or 0) < min_km:
            continue
        if max_km is not None and (r.get("distance_km") or 0) > max_km:
            continue
        if loop_only and not r.get("loop"):
            continue
        if lap_only and not r.get("lap_route"):
            continue
        if max_difficulty is not None and (r.get("difficulty_score") or 0) > max_difficulty:
            continue
        if search_lc and search_lc not in (r.get("name") or "").lower():
            continue
        out.append(r)
    return out

_CAT_WORDS = {
    "cat5": "gentle territory",
    "cat4": "hilly territory",
    "cat3": "serious climbing",
    "cat2": "big climb day",
    "cat1": "brutal territory",
    "hc": "epic mountain day",
}

def _score_route_for_suggest(
    r: dict,
    *,
    d_mid: float,
    d_half: float,
    target_diff_mid: float,
    diff_max: float | None,
    surface_filter: set[str] | None,
    finish_filter: set[str] | None,
) -> tuple[float, str]:
    """Compute match score (0..1) and a short conversational rationale.

    Improvements vs the naive v1 (per grill B):
    - Distance decay is quadratic with a 2.5 km floor on d_half so narrow
      bands still produce a usable ranking curve instead of a cliff.
    - Difficulty is rescaled against the user's cap (diff_max) and treated
      as "no preference" when cap is ≥9 so climbs and flats both surface.
    - Rationale is conversational ("Great fit …") with climb count + grade
      hints so cards feel less robotic.
    """
    actual_km = r.get("distance_km") or 0

    # --- Distance: quadratic decay, floored AND capped half-width.
    # Floor (2.5) prevents cliff on narrow bands; cap (30) prevents wide
    # bands like 10-200 km from scoring everything ≈1.0.
    d_half_eff = max(d_half, 2.5)
    d_half_eff = min(d_half_eff, 30.0)
    delta = abs(actual_km - d_mid)
    if delta >= d_half_eff * 2.0:
        distance_fit = 0.0
    else:
        # Quadratic: 1 - (delta/half)^2, so falloff is gentler near target,
        # steeper at edges. Hits 0 at 2*half (well outside the hard filter).
        t = delta / d_half_eff
        distance_fit = max(0.0, 1.0 - t * t)

    # --- Difficulty: rescaled against user cap, smooth fade toward 1.0
    # as diff_max approaches 10 ("no preference"). Avoids the hard cliff
    # that used to flip the whole ranking at diff_max=9.0.
    diff_score = r.get("difficulty_score") or 0
    if diff_max is None:
        difficulty_fit = 1.0
    else:
        band = max(1.0, float(diff_max))
        raw = max(0.0, min(1.0, 1.0 - abs(diff_score - target_diff_mid) / band))
        fade = max(0.0, min(1.0, (diff_max - 7.0) / 3.0))  # 7→0, 10→1
        difficulty_fit = raw + (1.0 - raw) * fade

    ps = (r.get("primary_surface") or "").lower()
    if surface_filter is None:
        surface_match = 1.0
    else:
        # Match if primary surface is selected, OR the route carries a
        # has_X flag for a selected surface, OR the surface mix has ≥30%
        # in any selected surface. (Previously only primary_surface was
        # checked, so a 70%-gravel asphalt-primary route scored 0.5 even
        # with surface=["gravel"].)
        mix = r.get("surface_mix_pct") or {}
        in_primary = ps in surface_filter
        in_flag = (
            ("gravel" in surface_filter and r.get("has_gravel")) or
            ("cobble" in surface_filter and r.get("has_cobble"))
        )
        in_mix = any((mix.get(s) or 0) >= 30 for s in surface_filter)
        # Pure surface match (primary=X when user picked X) outranks a mixed
        # match by 0.15 so gravel-primary routes top "has_gravel + asphalt-
        # primary" mixes on a gravel query. Below that, in_flag/in_mix still
        # rank above outright non-matches.
        if in_primary:
            surface_match = 1.0
        elif in_flag or in_mix:
            surface_match = 0.85
        else:
            surface_match = 0.4

    ft = (r.get("finish_type") or "").lower()
    if finish_filter is None:
        finish_match = 1.0
    else:
        finish_match = 1.0 if ft in finish_filter else 0.5

    score = (
        0.50 * distance_fit
        + 0.25 * difficulty_fit
        + 0.10 * surface_match
        + 0.15 * finish_match
    )

    # --- Rationale: conversational + differential
    km_txt = f"{round(actual_km)} km"
    climb_m = int(r.get("climb_m") or 0)
    climb_count = int(r.get("climb_count") or 0)
    max_grade = float(r.get("max_grade") or 0)
    cat = (r.get("category") or "").lower()
    pieces = []
    # Distance framing
    if delta <= 2.0:
        pieces.append(f"spot on your {km_txt} target")
    elif delta <= d_half_eff:
        pieces.append(f"close to your target ({km_txt})")
    else:
        pieces.append(f"{km_txt} ride")
    # Climb framing
    if climb_count >= 3 and max_grade >= 8:
        pieces.append(f"{climb_count} real climbs, max {max_grade:.0f}%")
    elif climb_m >= 300:
        pieces.append(f"{climb_m} m of climbing")
    elif cat in ("flat",) and climb_m < 150:
        pieces.append("mostly flat")
    elif cat and cat != "flat":
        # Map internal category tokens to human words so users don't see
        # "cat5 territory" on cards.
        pieces.append(_CAT_WORDS.get(cat, f"{cat} territory"))
    # Surface framing (only call out when user selected it)
    if surface_filter and ps in surface_filter:
        pieces.append(f"{ps} as requested")
    elif ps in ("gravel", "cobble"):
        pieces.append(f"{ps} surface")
    # Combine — keep it under ~14 words
    sentence = "Good fit: " + ", ".join(pieces[:3]) + "."
    # Use the SAME recomputed Z2 pace as the summary (23 km/h) so the card
    # and the rationale agree. Previously the rationale quoted the stored
    # field, which diverged from the summary's recomputed value for ~60% of
    # routes and caused "64 min" on the card next to "~81 min at Z2" in the
    # sentence. We derive here from distance rather than reading the summary
    # because the scorer predates the projection step.
    if actual_km > 0:
        recomputed_min = int(round(actual_km / 23.0 * 60))
        diff_tail = f" {diff_score:.1f}/10 difficulty · ~{recomputed_min} min at Z2."
    else:
        diff_tail = f" {diff_score:.1f}/10 difficulty."
    return round(score, 3), sentence + diff_tail

def _top_archetypes_for_region(routes_in_region: list[dict], n: int = 5) -> list[str]:
    """Return up to N representative tags for a region (most common first)."""
    from collections import Counter
    tag_counts: Counter[str] = Counter()
    for r in routes_in_region:
        for t in r.get("tags", []) or []:
            tag_counts[t] += 1
    return [t for t, _ in tag_counts.most_common(n)]

def _route_profile_points(lat_lon_grade: list) -> list[dict]:
    """Convert [[lat,lon,grade],...] to [{d, e, g},...] for elevProfile rendering."""
    from geodesy import haversine
    if not lat_lon_grade or len(lat_lon_grade) < 2:
        return []

    points = []
    cum_dist_km = 0.0
    cum_ele = 0.0
    for i, pt in enumerate(lat_lon_grade):
        if len(pt) < 3:
            continue
        lat, lon, grade = pt[0], pt[1], pt[2]
        grade = max(-45, min(45, grade))  # cap to realistic range
        if i > 0:
            prev = lat_lon_grade[i - 1]
            d_m = haversine((prev[0], prev[1]), (lat, lon))  # metres
            d_km = d_m / 1000.0
            cum_dist_km += d_km
            cum_ele += d_m * grade / 100.0  # elevation change in metres
        points.append({"d": round(cum_dist_km, 3), "e": round(cum_ele, 1), "g": round(grade, 1)})

    # Downsample to max 200 points
    if len(points) > 200:
        step = max(1, len(points) // 200)
        sampled = [points[i] for i in range(0, len(points), step)]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        return sampled
    return points

def _load_route_index() -> dict:
    """Load compact pre-indexed virtual route profiles (~1MB, loaded once)."""
    return cached("route_index", lambda: (
        json.loads(ROUTE_PROFILES_INDEX.read_text(encoding="utf-8"))
        if ROUTE_PROFILES_INDEX.exists() else {}
    ), ttl=3600)

def _load_route_detail(url: str) -> dict | None:
    """Load full route data from individual file (for detail modal).

    Profile files are named ``<world>__<slug>.json`` (see
    ``generate_route_profiles.py``). The frontend may pass several URL shapes,
    so we try them all:

    * ``<world>/<slug>``                      (new canonical form)
    * ``virtual/<world>/<file>.crs``          (CRS path for virtual worlds)
    * ``<region>/<File Name>.crs``            (CRS path for real-world rides)
    * ``/climb-portal/<slug>`` or ``/route/<world>/<slug>``  (legacy)
    * bare ``<slug>``                         (last resort)
    """
    if not url:
        return None
    # Strip query strings / fragments and sanitise
    url = url.split("?", 1)[0].split("#", 1)[0].strip()
    clean = url.replace("..", "").replace("\\", "/").strip("/")
    parts = [p for p in clean.split("/") if p]
    if not parts:
        return None

    last = parts[-1]
    # Strip file extension, if any
    last_stem = last.rsplit(".", 1)[0] if "." in last else last

    # Build candidate (world, slug) pairs to try, in priority order.
    candidates: list[tuple[str, str]] = []

    # Legacy: "climb-portal/<slug>" anywhere in path
    if "climb-portal" in parts:
        candidates.append(("climb-portal", last_stem))

    # Virtual routes: "virtual/<world>/<world>__<slug>.crs"
    if len(parts) >= 3 and parts[0] == "virtual":
        world = parts[1]
        slug = last_stem.split("__", 1)[1] if "__" in last_stem else _slugify_route_name(last_stem)
        candidates.append((world, slug))

    # Legacy "/route/<world>/<slug>"
    if "route" in parts:
        try:
            idx = parts.index("route")
            if idx + 2 < len(parts):
                candidates.append((parts[idx + 1], parts[idx + 2]))
        except ValueError:
            pass

    # New canonical form: "<world>/<slug>" (exact match to index keys)
    if len(parts) == 2:
        candidates.append((parts[0], parts[1]))

    # CRS path for real-world rides: "<region>/<File Name>.crs"
    if len(parts) >= 2:
        world = parts[-2]
        # When the filename embeds "<world>__<slug>" use that slug directly,
        # otherwise slugify the whole stem (matches generate_route_profiles.py).
        if "__" in last_stem:
            slug = last_stem.split("__", 1)[1]
        else:
            slug = _slugify_route_name(last_stem)
        candidates.append((world, slug))

    # Bare slug (no world): fall back to any file whose stem contains the slug.
    # Handled after the direct lookups below.

    seen: set[tuple[str, str]] = set()
    for world, slug in candidates:
        if not world or not slug:
            continue
        # Sanitise — files are on disk so block traversal
        world_safe = re.sub(r"[^A-Za-z0-9_\-]", "", world)
        slug_safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
        key = (world_safe, slug_safe)
        if key in seen:
            continue
        seen.add(key)
        path = ROUTE_PROFILES_DIR / f"{world_safe}__{slug_safe}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    # Fallback: scan for any profile whose filename contains the last slug.
    # This handles ambiguous inputs like a bare slug with no world prefix.
    if last_stem and ROUTE_PROFILES_DIR.exists():
        # If the bare stem is itself "<world>__<slug>", split on the first
        # "__" and try that pair directly.
        if "__" in last_stem:
            world_guess, slug_guess = last_stem.split("__", 1)
            world_safe = re.sub(r"[^A-Za-z0-9_\-]", "", world_guess).replace("-", "_")
            slug_safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug_guess)
            path = ROUTE_PROFILES_DIR / f"{world_safe}__{slug_safe}.json"
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
        fallback_slug = _slugify_route_name(last_stem)
        if fallback_slug:
            needle = f"__{fallback_slug}.json"
            for candidate in ROUTE_PROFILES_DIR.glob(f"*{needle}"):
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
    return None

