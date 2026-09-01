#!/usr/bin/env python3
"""
osm_surface_mapper.py
=====================

Derive per-segment surface classification (asphalt / gravel / cobble / unknown)
for real-world cycling routes by matching GPX trackpoints against OpenStreetMap
highway data retrieved from the Overpass API.

Overview
--------
1.  Parse a GPX file -> list of (lat, lon) trackpoints.
2.  Build a bounding box that wraps the track (+ ~1 km buffer).
3.  Query Overpass for every `highway=*` way that has any of:
        - an explicit `surface` tag
        - `tracktype`
        - `highway` in {track, path, cycleway, bridleway, unclassified}
    Overpass returns full geometries via `out geom;`.
4.  For every trackpoint, find the nearest OSM way.  Distance from a point to
    a polyline is the minimum distance to any of its segments.  We use a
    bucketed spatial index (lat/lon grid at ~200 m resolution) so this is
    O(N * k) rather than O(N * M).
5.  Classify each point: asphalt / gravel / cobble / unknown based on the tags
    on the matched way (rules below).  Points with no way inside 50 m are
    `unknown`.
6.  Emit:
        - `surface_segments` -> list of {start_km, end_km, surface}, merging
          consecutive same-surface points.
        - `surface_mix_pct`  -> {asphalt, gravel, cobble, unknown} summing to 100.

Usage
-----

Single file (prints a summary, also writes a cache entry):

    python scripts/osm_surface_mapper.py path/to/file.gpx

Batch mode (walks gpx_sources/*, updates routes.json in place):

    python scripts/osm_surface_mapper.py --batch
    python scripts/osm_surface_mapper.py --batch --limit 20
    python scripts/osm_surface_mapper.py --batch --only amsterdam,col-du-vam

Cache
-----
Results are cached as JSON in gpx_sources/.osm_cache/<slug>.json so repeated
runs don't hammer Overpass.  Delete a file to force a re-query.

Dependencies: stdlib + requests only.  No new packages.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import requests
except ImportError:  # pragma: no cover - requests is already a project dep
    print("ERROR: requests library required (pip install requests)", file=sys.stderr)
    sys.exit(2)


# ------------------------------------------------------------------ config ---

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GPX_SOURCES = PROJECT_ROOT / "gpx_sources"
CACHE_DIR = GPX_SOURCES / ".osm_cache"
ROUTES_JSON = PROJECT_ROOT / "routes.json"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/cgi/interpreter",
]

BBOX_BUFFER_KM = 1.0          # expand trackpoint bbox by this much before query
MATCH_RADIUS_M = 50.0         # if nearest way > this, point is `unknown`
GRID_CELL_M = 200.0           # spatial index cell size (metres)
OVERPASS_TIMEOUT_S = 90       # HTTP timeout for Overpass request
OVERPASS_MAX_RETRIES = 5      # exponential backoff on 429/502/503
BATCH_MAX_WORKERS = 6         # concurrent GPX files; Overpass tolerates 4-8

# Round-robin endpoint selector (thread-safe via itertools.cycle + lock).
_ENDPOINT_CYCLE = itertools.cycle(range(len(OVERPASS_ENDPOINTS)))
_ENDPOINT_LOCK = threading.Lock()
# Serialise stdout so interleaved worker lines don't get shredded.
_PRINT_LOCK = threading.Lock()


def _next_endpoint_idx() -> int:
    with _ENDPOINT_LOCK:
        return next(_ENDPOINT_CYCLE)


def _log(msg: str, err: bool = False) -> None:
    with _PRINT_LOCK:
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)


# --------------------------------------------------------- surface classify ---

ASPHALT_SURFACES = {
    "asphalt", "paved", "concrete", "concrete:lanes", "concrete:plates",
    "chipseal", "bitumen", "tarmac", "metal", "asphalt_concrete",
    # smooth engineered pavers — ride like asphalt on a gravel bike
    "paving_stones", "paving_stones:lanes",
}

COBBLE_SURFACES = {
    "cobblestone", "cobblestone:flattened", "sett", "unhewn_cobblestone",
    "stone", "stones", "rock",
}

GRAVEL_SURFACES = {
    "gravel", "fine_gravel", "compacted", "unpaved", "dirt", "ground",
    "earth", "mud", "sand", "pebblestone", "grass", "grass_paver", "wood",
    "woodchips", "clay", "shells", "salt",
}

GRAVEL_HIGHWAY_TYPES_IF_NO_SURFACE = {"track", "path", "bridleway"}

# Paved-by-default highway types in most of Europe.  If a way has one of these
# tags and no `surface` tag, we treat it as asphalt.  OSM surface-tagging is
# very patchy so relying only on explicit tags produces 50%+ `unknown` even in
# well-mapped Dutch cities.  We therefore include residential/service/
# unclassified/living_street here — false positives (e.g. cobbled old-town
# streets) are real but relatively rare, while false negatives (marking a
# regular asphalt road as `unknown`) are very common.
ASPHALT_HIGHWAY_TYPES_IF_NO_SURFACE = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "residential", "living_street", "service",
    "unclassified", "pedestrian", "cycleway",
}

# Ordered preference if a way has multiple surface-like tags: prefer the
# `surface` value itself, fall back to `tracktype`/highway heuristics.
TRACKTYPE_TO_SURFACE = {
    "grade1": "compacted",   # solid — reads as gravel per our taxonomy
    "grade2": "compacted",
    "grade3": "unpaved",
    "grade4": "ground",
    "grade5": "ground",
}


def classify_way(tags: dict) -> str:
    """Return one of asphalt|gravel|cobble|unknown given a way's tags."""
    if not tags:
        return "unknown"

    surface = (tags.get("surface") or "").strip().lower()
    if surface in ASPHALT_SURFACES:
        return "asphalt"
    if surface in COBBLE_SURFACES:
        return "cobble"
    if surface in GRAVEL_SURFACES:
        return "gravel"

    # No explicit surface — try tracktype.
    tracktype = (tags.get("tracktype") or "").strip().lower()
    if tracktype in TRACKTYPE_TO_SURFACE:
        proxy = TRACKTYPE_TO_SURFACE[tracktype]
        if proxy in ASPHALT_SURFACES:
            return "asphalt"
        if proxy in COBBLE_SURFACES:
            return "cobble"
        if proxy in GRAVEL_SURFACES:
            return "gravel"

    # Highway-type heuristic for the unpaved family.
    highway = (tags.get("highway") or "").strip().lower()
    if highway in GRAVEL_HIGHWAY_TYPES_IF_NO_SURFACE:
        return "gravel"
    if highway in ASPHALT_HIGHWAY_TYPES_IF_NO_SURFACE:
        return "asphalt"
    # residential/service/unclassified/living_street/pedestrian/footway
    # are genuinely ambiguous — leave as unknown rather than guess.
    return "unknown"


# ------------------------------------------------------------- GPX parsing ---

GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def parse_gpx(gpx_path: Path) -> list[tuple[float, float]]:
    """Return a flat list of (lat, lon) in track order.  Raises on malformed GPX.

    Accepts both ``<trkpt>`` (track points, the common Strava/Wahoo format)
    and ``<rtept>`` (route points, emitted by onthegomap.com and some
    editors).  Tries the namespaced form first, falls back to bare tags so
    that creators who dropped the xmlns still parse.
    """
    tree = ET.parse(str(gpx_path))
    root = tree.getroot()
    pts: list[tuple[float, float]] = []
    tag_variants = [
        "{http://www.topografix.com/GPX/1/1}trkpt",
        "{http://www.topografix.com/GPX/1/1}rtept",
        "trkpt",
        "rtept",
    ]
    for tag in tag_variants:
        for el in root.iter(tag):
            try:
                pts.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
            except (KeyError, ValueError):
                continue
        if pts:
            break
    return pts


# ------------------------------------------------------------------- geom ---

# Canonical haversine lives in geodesy.py; import with a repo-root fallback
# so ``python scripts/osm_surface_mapper.py`` works regardless of CWD.
try:
    from geodesy import EARTH_RADIUS_M as EARTH_R_M, haversine as _haversine
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from geodesy import EARTH_RADIUS_M as EARTH_R_M, haversine as _haversine


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Thin wrapper for legacy call sites."""
    return _haversine((lat1, lon1), (lat2, lon2))


def latlon_to_local_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Project (lat, lon) to a local ENU-ish metre-grid anchored at (lat0, lon0).

    Good enough for distances of a few km and for the point-to-segment
    distance calculation we do per trackpoint.  Latitude → metres uses the
    mean-earth radius; longitude scales by cos(lat0).
    """
    dx = math.radians(lon - lon0) * EARTH_R_M * math.cos(math.radians(lat0))
    dy = math.radians(lat - lat0) * EARTH_R_M
    return dx, dy


def point_to_segment_dist_m(px: float, py: float,
                            ax: float, ay: float,
                            bx: float, by: float) -> float:
    """Min distance (metres) from point P to segment A-B, all in local xy."""
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    if ab2 == 0.0:
        dx, dy = px - ax, py - ay
        return math.hypot(dx, dy)
    t = (apx * abx + apy * aby) / ab2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    cx = ax + t * abx
    cy = ay + t * aby
    return math.hypot(px - cx, py - cy)


# ------------------------------------------------------- Overpass querying ---


@dataclass
class OverpassWay:
    way_id: int
    tags: dict
    # Nodes projected into local xy (metres) for fast distance math.
    xy: list[tuple[float, float]] = field(default_factory=list)


def build_overpass_query(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    # Two alternations: (a) anything with a `surface` tag, (b) unpaved-ish
    # highway types regardless of whether they carry `surface`.
    # We want every rideable highway, not just those with a `surface` tag.
    # Explicit-surface entries are redundant (they're already covered by the
    # broad `way["highway"]` alternation) but we keep the decomposition so
    # it's easy to narrow later if the payload ever gets too big.
    return (
        "[out:json][timeout:60];"
        "("
        f'way["highway"]["surface"]({bbox});'
        f'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|'
        f'residential|living_street|unclassified|service|cycleway|track|'
        f'path|bridleway|footway|pedestrian)$"]({bbox});'
        ");"
        "out geom;"
    )


def overpass_request(query: str, endpoint_idx: int | None = None) -> dict:
    """POST to Overpass, retrying on common soft-failures.  Raises on final error.

    When running under a thread pool, each call picks its starting endpoint
    via round-robin (``_next_endpoint_idx``) so concurrent workers naturally
    spread load across the mirrors in OVERPASS_ENDPOINTS.  On 429/503 we
    back off with a random 5-15s jitter (avoids thundering-herd retries
    from 6 workers hitting the same endpoint at once) and rotate mirror.
    """
    if endpoint_idx is None:
        endpoint_idx = _next_endpoint_idx()
    attempt = 0
    last_exc: Exception | None = None
    endpoint = OVERPASS_ENDPOINTS[endpoint_idx % len(OVERPASS_ENDPOINTS)]

    while attempt < OVERPASS_MAX_RETRIES:
        attempt += 1
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                timeout=OVERPASS_TIMEOUT_S,
                headers={"User-Agent": "health_tracker-surface-mapper/1.0"},
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 502, 503, 504):
                # Rotate endpoint and back off with jitter.
                endpoint_idx = (endpoint_idx + 1) % len(OVERPASS_ENDPOINTS)
                endpoint = OVERPASS_ENDPOINTS[endpoint_idx]
                delay = random.uniform(5.0, 15.0)
                _log(f"  [overpass] HTTP {resp.status_code}, retrying in {delay:.1f}s (endpoint -> {endpoint})", err=True)
                time.sleep(delay)
                continue
            # Other HTTP error — unrecoverable.
            raise RuntimeError(f"Overpass HTTP {resp.status_code}: {resp.text[:300]}")
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            delay = random.uniform(5.0, 15.0)
            _log(f"  [overpass] {type(exc).__name__}: {exc}; retrying in {delay:.1f}s", err=True)
            time.sleep(delay)
            endpoint_idx = (endpoint_idx + 1) % len(OVERPASS_ENDPOINTS)
            endpoint = OVERPASS_ENDPOINTS[endpoint_idx]
        except ValueError as exc:
            # JSON decode error — treat as transient.
            last_exc = exc
            delay = random.uniform(5.0, 15.0)
            _log(f"  [overpass] bad JSON: {exc}; retrying in {delay:.1f}s", err=True)
            time.sleep(delay)

    raise RuntimeError(f"Overpass failed after {OVERPASS_MAX_RETRIES} attempts: {last_exc}")


def fetch_ways(south: float, west: float, north: float, east: float,
               lat0: float, lon0: float) -> list[OverpassWay]:
    """Query Overpass for the bbox and project each way's geometry to local xy.

    Overpass will occasionally return HTTP 200 with an empty `elements`
    array when the upstream query timed out server-side.  Zero ways in a
    populated bbox is almost always a silent failure rather than a real
    empty region (we buffer the bbox by 1 km, so even a remote trail
    should intersect *something*).  We retry once with a fresh endpoint
    before accepting the empty answer.
    """
    query = build_overpass_query(south, west, north, east)
    data = overpass_request(query)
    ways = _parse_overpass_ways(data, lat0, lon0)
    if not ways:
        _log("  [overpass] empty response; retrying once on a fresh endpoint", err=True)
        data = overpass_request(query, endpoint_idx=_next_endpoint_idx())
        ways = _parse_overpass_ways(data, lat0, lon0)
    return ways


def _parse_overpass_ways(data: dict, lat0: float, lon0: float) -> list[OverpassWay]:
    ways: list[OverpassWay] = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        xy = [latlon_to_local_xy(pt["lat"], pt["lon"], lat0, lon0) for pt in geom]
        ways.append(OverpassWay(way_id=int(el["id"]),
                                tags=dict(el.get("tags") or {}),
                                xy=xy))
    return ways


# ---------------------------------------------------------- spatial index ---


class WayGrid:
    """Bucket way segments into a regular xy grid so we can look up candidates
    without scanning every way for every point."""

    def __init__(self, ways: list[OverpassWay], cell_m: float = GRID_CELL_M):
        self.cell = cell_m
        # cells[(ix, iy)] = list of (way_index, seg_index)
        self.cells: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for wi, w in enumerate(ways):
            for si in range(len(w.xy) - 1):
                ax, ay = w.xy[si]
                bx, by = w.xy[si + 1]
                # Fill every cell the segment's bbox touches (cheap vs. true
                # rasterisation, and the lookup also checks a 3x3 neighbourhood).
                xmin, xmax = sorted((ax, bx))
                ymin, ymax = sorted((ay, by))
                ix0 = int(math.floor(xmin / cell_m))
                ix1 = int(math.floor(xmax / cell_m))
                iy0 = int(math.floor(ymin / cell_m))
                iy1 = int(math.floor(ymax / cell_m))
                for ix in range(ix0, ix1 + 1):
                    for iy in range(iy0, iy1 + 1):
                        self.cells[(ix, iy)].append((wi, si))

    def candidates(self, px: float, py: float, radius_m: float) -> Iterable[tuple[int, int]]:
        """Yield (way_index, seg_index) for every segment whose cell is within
        radius of point (px, py).  May return duplicates — the caller dedupes
        implicitly via min()."""
        reach = int(math.ceil(radius_m / self.cell))
        ix0 = int(math.floor(px / self.cell))
        iy0 = int(math.floor(py / self.cell))
        seen = set()
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                key = (ix0 + dx, iy0 + dy)
                bucket = self.cells.get(key)
                if not bucket:
                    continue
                for wi_si in bucket:
                    if wi_si in seen:
                        continue
                    seen.add(wi_si)
                    yield wi_si


# --------------------------------------------------------- main classifier ---


def bbox_with_buffer(points: list[tuple[float, float]], buffer_km: float) -> tuple[float, float, float, float]:
    """Return (south, west, north, east) padded by buffer_km on each side."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    s, n = min(lats), max(lats)
    w, e = min(lons), max(lons)
    # 1 km of latitude ~= 1/111 deg; longitude scales with cos(lat).
    dlat = buffer_km / 111.0
    mean_lat = (s + n) / 2
    dlon = buffer_km / (111.0 * max(0.01, math.cos(math.radians(mean_lat))))
    return (s - dlat, w - dlon, n + dlat, e + dlon)


def classify_track(points: list[tuple[float, float]], ways: list[OverpassWay],
                   lat0: float, lon0: float,
                   match_radius_m: float = MATCH_RADIUS_M) -> tuple[list[str], list[float]]:
    """Return (per-point classification, per-point cumulative_distance_km).

    ``lat0``/``lon0`` MUST be the same anchor the ways were projected against
    (see ``fetch_ways``), otherwise the distance math will silently wander
    off by tens of metres and most points will fall outside ``match_radius_m``.
    """
    if not points:
        return [], []

    grid = WayGrid(ways)

    # Cumulative distance (km) along the track, computed with haversine so
    # it's accurate even for long routes.
    cum_km = [0.0]
    for i in range(1, len(points)):
        d = haversine_m(points[i - 1][0], points[i - 1][1],
                        points[i][0], points[i][1])
        cum_km.append(cum_km[-1] + d / 1000.0)

    labels: list[str] = []
    for lat, lon in points:
        px, py = latlon_to_local_xy(lat, lon, lat0, lon0)
        best_dist = float("inf")
        best_wi: int | None = None
        for wi, si in grid.candidates(px, py, match_radius_m):
            ax, ay = ways[wi].xy[si]
            bx, by = ways[wi].xy[si + 1]
            d = point_to_segment_dist_m(px, py, ax, ay, bx, by)
            if d < best_dist:
                best_dist = d
                best_wi = wi
                if d < 1.0:
                    break  # effectively on the centreline
        if best_wi is None or best_dist > match_radius_m:
            labels.append("unknown")
        else:
            labels.append(classify_way(ways[best_wi].tags))
    return labels, cum_km


def build_segments(labels: list[str], cum_km: list[float]) -> list[dict]:
    """Merge consecutive same-label points into segments keyed by cumulative km."""
    if not labels:
        return []
    segments: list[dict] = []
    cur = labels[0]
    seg_start = cum_km[0]
    for i in range(1, len(labels)):
        if labels[i] != cur:
            segments.append({
                "start_km": round(seg_start, 3),
                "end_km": round(cum_km[i], 3),
                "surface": cur,
            })
            cur = labels[i]
            seg_start = cum_km[i]
    segments.append({
        "start_km": round(seg_start, 3),
        "end_km": round(cum_km[-1], 3),
        "surface": cur,
    })
    return segments


def compute_mix_pct(segments: list[dict]) -> dict:
    """Distance-weighted mix across segments, as percentages summing to 100."""
    totals = {"asphalt": 0.0, "gravel": 0.0, "cobble": 0.0, "unknown": 0.0}
    for seg in segments:
        length = max(0.0, seg["end_km"] - seg["start_km"])
        totals[seg["surface"]] = totals.get(seg["surface"], 0.0) + length
    total = sum(totals.values())
    if total <= 0:
        return {k: 0.0 for k in totals}
    pct = {k: round(v / total * 100.0, 1) for k, v in totals.items()}
    # Fix rounding drift so it sums to 100.
    drift = round(100.0 - sum(pct.values()), 1)
    if drift != 0:
        largest = max(pct, key=pct.get)
        pct[largest] = round(pct[largest] + drift, 1)
    return pct


# ------------------------------------------------------- orchestrator API ---


def process_gpx(gpx_path: Path, use_cache: bool = True,
                verbose: bool = True) -> dict:
    """Full pipeline: parse GPX -> fetch OSM -> classify -> return result dict.

    Result shape:
        {
            "slug": str,
            "gpx_path": str,
            "num_points": int,
            "distance_km": float,
            "query_seconds": float,
            "surface_mix_pct": {...},
            "surface_segments": [...],
            "ways_fetched": int,
        }
    """
    slug = gpx_path.stem
    cache_path = CACHE_DIR / f"{slug}.json"
    if use_cache and cache_path.exists():
        try:
            with cache_path.open() as f:
                cached = json.load(f)
            if verbose:
                print(f"[cache] {slug} -> {cache_path.name}")
            return cached
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [cache] {slug} unreadable ({exc}); re-querying", file=sys.stderr)

    points = parse_gpx(gpx_path)
    if not points:
        raise ValueError(f"No trackpoints in {gpx_path}")

    south, west, north, east = bbox_with_buffer(points, BBOX_BUFFER_KM)
    lat0 = (south + north) / 2
    lon0 = (west + east) / 2

    if verbose:
        print(f"[overpass] {slug}: bbox=({south:.3f},{west:.3f})-({north:.3f},{east:.3f}), {len(points)} pts")
    t0 = time.time()
    ways = fetch_ways(south, west, north, east, lat0, lon0)
    query_seconds = time.time() - t0
    if verbose:
        print(f"  -> {len(ways)} ways in {query_seconds:.1f}s")

    labels, cum_km = classify_track(points, ways, lat0, lon0)
    segments = build_segments(labels, cum_km)
    mix_pct = compute_mix_pct(segments)

    result = {
        "slug": slug,
        "gpx_path": str(gpx_path.relative_to(PROJECT_ROOT)),
        "num_points": len(points),
        "distance_km": round(cum_km[-1] if cum_km else 0.0, 3),
        "query_seconds": round(query_seconds, 2),
        "surface_mix_pct": mix_pct,
        "surface_segments": segments,
        "ways_fetched": len(ways),
    }

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w") as f:
            json.dump(result, f, indent=2)
    except OSError as exc:
        print(f"  [cache] write failed ({exc})", file=sys.stderr)

    # No per-query sleep: concurrency is throttled by the thread pool size
    # (BATCH_MAX_WORKERS) and 429/503 responses are handled by the retry loop
    # with random 5-15s jitter, which is the correct backpressure mechanism.
    return result


# -------------------------------------------------------------- batch mode ---


def iter_all_gpx(dirs: list[str] | None = None) -> list[Path]:
    """All GPX files under gpx_sources/ (excluding the cache).

    If ``dirs`` is given, only those subdirectories are walked.  This is
    used to scope the batch to specific collections (e.g. netherlands_gravel
    + gravel_europe) without picking up newly-added dirs the caller isn't
    ready to process.
    """
    out: list[Path] = []
    for sub in sorted(GPX_SOURCES.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        if dirs is not None and sub.name not in dirs:
            continue
        out.extend(sorted(sub.glob("*.gpx")))
    return out


def find_route_entry(routes: list[dict], slug: str) -> dict | None:
    """Route ids look like `netherlands_gravel/<slug>` — match either side."""
    target = slug.lower()
    for r in routes:
        rid = (r.get("id") or "").lower()
        if rid.endswith("/" + target):
            return r
        if rid == target:
            return r
    return None


def recompute_primary(mix: dict) -> str:
    """Pick the primary surface from the enriched mix (ignore `unknown`)."""
    ranked = sorted(
        ((k, v) for k, v in mix.items() if k != "unknown"),
        key=lambda kv: kv[1],
        reverse=True,
    )
    if not ranked or ranked[0][1] <= 0:
        return "asphalt"
    return ranked[0][0]


def _process_one_for_batch(gpx: Path, idx: int, total: int) -> dict:
    """Worker wrapper: returns a dict of either {result, cached} or {error}.

    Runs in a ThreadPoolExecutor; must be self-contained.  Catches its own
    exceptions so a single bad GPX doesn't sink the batch.
    """
    slug = gpx.stem
    cached = (CACHE_DIR / f"{slug}.json").exists()
    t0 = time.time()
    try:
        result = process_gpx(gpx, use_cache=True, verbose=False)
    except ValueError as exc:
        _log(f"[{idx}/{total}] {gpx.parent.name}/{gpx.name}  !! GPX error: {exc}")
        return {"gpx": gpx, "slug": slug, "error": "gpx", "exc": str(exc)}
    except Exception as exc:
        _log(f"[{idx}/{total}] {gpx.parent.name}/{gpx.name}  !! Overpass error: {exc}")
        return {"gpx": gpx, "slug": slug, "error": "overpass", "exc": str(exc)}
    elapsed = time.time() - t0
    tag = "cache" if cached else f"overpass {result.get('query_seconds', 0):.1f}s"
    _log(f"[{idx}/{total}] {gpx.parent.name}/{gpx.name}  ({tag}, {elapsed:.1f}s wall)")
    return {"gpx": gpx, "slug": slug, "cached": cached, "result": result, "wall": elapsed}


def run_batch(limit: int | None = None, only: set[str] | None = None,
              dry_run: bool = False, workers: int = BATCH_MAX_WORKERS,
              dirs: list[str] | None = None) -> dict:
    """Iterate GPX files in parallel, enrich matching routes.json entries, write back.

    Parallelism:
        ThreadPoolExecutor with ``workers`` (default 6).  Overpass tolerates
        4-8 concurrent queries from one IP; 6 is the sweet spot.  Cache hits
        return almost instantly, so on re-runs the pool mostly stays idle.

    Thread-safety model:
        Workers only read cache / hit Overpass / write their per-slug cache
        file.  All mutation of ``routes`` and ``stats`` happens on the main
        thread after ``as_completed`` yields a future.
    """
    all_gpx = iter_all_gpx(dirs=dirs)
    if only:
        all_gpx = [p for p in all_gpx if p.stem in only]

    if not all_gpx:
        print("no GPX files selected")
        return {"processed": 0}

    print(f"loading {ROUTES_JSON} ...")
    with ROUTES_JSON.open() as f:
        routes = json.load(f)

    # Filter to GPX files that actually have a corresponding route entry.
    # Roughly a third of the GPX corpus (france_gravel/, germany_gravel/,
    # italy_gravel/, usa_gravel/, plus a few one-offs) has no matching
    # routes.json entry yet — Overpass-querying those is pure waste.  When
    # the caller pins specific slugs via --only we trust them and skip the
    # filter so a cache entry can be primed ahead of adding the route.
    if not only:
        matched = [p for p in all_gpx if find_route_entry(routes, p.stem) is not None]
        orphans = [p for p in all_gpx if find_route_entry(routes, p.stem) is None]
        all_gpx = matched
        if orphans:
            print(f"skipping {len(orphans)} GPX file(s) with no matching route entry "
                  f"(e.g. {', '.join(p.stem for p in orphans[:5])}"
                  f"{'...' if len(orphans) > 5 else ''})")

    if limit is not None:
        all_gpx = all_gpx[:limit]

    stats = {
        "selected": len(all_gpx),
        "enriched": 0,
        "cache_hits": 0,
        "overpass_calls": 0,
        "no_route_match": 0,
        "gpx_errors": 0,
        "overpass_errors": 0,
        "query_seconds_total": 0.0,
        "query_seconds_min": float("inf"),
        "query_seconds_max": 0.0,
        "unmatched_routes": [],
    }

    total = len(all_gpx)
    print(f"dispatching {total} GPX files across {workers} workers (endpoints={len(OVERPASS_ENDPOINTS)})\n")

    batch_t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_process_one_for_batch, gpx, i, total): gpx
            for i, gpx in enumerate(all_gpx, 1)
        }
        for fut in as_completed(futures):
            res = fut.result()
            slug = res["slug"]
            if res.get("error"):
                if res["error"] == "gpx":
                    stats["gpx_errors"] += 1
                else:
                    stats["overpass_errors"] += 1
                continue

            result = res["result"]
            if res["cached"]:
                stats["cache_hits"] += 1
            else:
                stats["overpass_calls"] += 1
                qs = float(result.get("query_seconds", 0.0))
                stats["query_seconds_total"] += qs
                stats["query_seconds_min"] = min(stats["query_seconds_min"], qs)
                stats["query_seconds_max"] = max(stats["query_seconds_max"], qs)

            route = find_route_entry(routes, slug)
            if route is None:
                _log(f"  !! no route in routes.json for slug '{slug}'", err=True)
                stats["no_route_match"] += 1
                stats["unmatched_routes"].append(slug)
                continue

            mix = result["surface_mix_pct"]
            route["surface_mix_pct"] = mix
            route["surface_segments"] = result["surface_segments"]
            route["surface_source"] = "osm_overpass"
            primary = recompute_primary(mix)
            route["primary_surface"] = primary
            route["has_gravel"] = mix.get("gravel", 0) >= 5.0
            route["has_cobble"] = mix.get("cobble", 0) >= 1.0
            stats["enriched"] += 1

    batch_elapsed = time.time() - batch_t0

    if not dry_run and stats["enriched"] > 0:
        # Write to routes.json.  Keep a .bak of the pre-run file the first time.
        bak = ROUTES_JSON.with_suffix(".json.surface_bak")
        if not bak.exists():
            bak.write_text(ROUTES_JSON.read_text())
            print(f"\nbacked up original to {bak.name}")
        with ROUTES_JSON.open("w") as f:
            json.dump(routes, f, indent=2)
        print(f"wrote {stats['enriched']} enriched routes to {ROUTES_JSON}")

    if stats["query_seconds_min"] == float("inf"):
        stats["query_seconds_min"] = 0.0

    print("\n==== batch summary ====")
    print(f"  wall_elapsed_s: {batch_elapsed:.1f}")
    print(f"  workers: {workers}")
    for k, v in stats.items():
        if k == "unmatched_routes":
            continue
        print(f"  {k}: {v}")
    if stats["unmatched_routes"]:
        print("  unmatched slugs:")
        for s in stats["unmatched_routes"]:
            print(f"    - {s}")
    if stats["overpass_calls"] > 0:
        avg = stats["query_seconds_total"] / stats["overpass_calls"]
        print(f"  avg Overpass query: {avg:.2f}s  (min {stats['query_seconds_min']:.1f}s, max {stats['query_seconds_max']:.1f}s)")
    stats["wall_elapsed_s"] = round(batch_elapsed, 1)
    return stats


# ----------------------------------------------------------------- CLI -----


def _print_single_result(result: dict, max_segs: int = 6) -> None:
    print(f"\n==== {result['slug']} ====")
    print(f"points={result['num_points']}  distance_km={result['distance_km']}  ways={result['ways_fetched']}")
    print(f"query_seconds={result['query_seconds']}s")
    print(f"surface_mix_pct: {result['surface_mix_pct']}")
    segs = result["surface_segments"]
    print(f"surface_segments: {len(segs)} total, first {min(max_segs, len(segs))}:")
    for seg in segs[:max_segs]:
        print(f"  [{seg['start_km']:.2f} .. {seg['end_km']:.2f} km]  {seg['surface']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("gpx", nargs="?", help="GPX file to process (single-file mode)")
    ap.add_argument("--batch", action="store_true", help="Run batch over gpx_sources/")
    ap.add_argument("--limit", type=int, help="Batch: max GPX files to process")
    ap.add_argument("--only", help="Batch: comma-sep list of slugs (gpx filename without .gpx)")
    ap.add_argument("--no-cache", action="store_true", help="Ignore cache, re-query Overpass")
    ap.add_argument("--dry-run", action="store_true", help="Batch: don't write routes.json")
    ap.add_argument("--workers", type=int, default=BATCH_MAX_WORKERS,
                    help=f"Batch: concurrent workers (default {BATCH_MAX_WORKERS}, Overpass tolerates 4-8)")
    ap.add_argument("--dirs", help=("Batch: comma-sep subdirs of gpx_sources/ to process "
                                     "(default: all). e.g. 'netherlands_gravel,gravel_europe'"))
    args = ap.parse_args(argv)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.batch:
        only = {s.strip() for s in args.only.split(",")} if args.only else None
        dirs = [s.strip() for s in args.dirs.split(",")] if args.dirs else None
        run_batch(limit=args.limit, only=only, dry_run=args.dry_run,
                  workers=args.workers, dirs=dirs)
        return 0

    if not args.gpx:
        ap.print_help()
        return 2

    gpx_path = Path(args.gpx).expanduser().resolve()
    if not gpx_path.exists():
        print(f"not found: {gpx_path}", file=sys.stderr)
        return 2

    result = process_gpx(gpx_path, use_cache=not args.no_cache)
    _print_single_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
