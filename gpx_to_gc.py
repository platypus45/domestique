"""
Convert GPX files to GoldenCheetah-ready format for slope simulation on Tacx Neo 2T.

Does three things:
  1. Parses GPX (lat, lon, elevation)
  2. Smooths elevation data (rolling average to remove GPS noise)
  3. Outputs .crs (Computrainer Course) files for GoldenCheetah simulation mode

Usage:
  python3 gpx_to_gc.py                                    # convert all in ~/Documents/GPX
  python3 gpx_to_gc.py --input ~/Documents/GPX/cyclinglocations/alps/
  python3 gpx_to_gc.py --input alpe_dhuez.gpx --smooth 10 # single file, 10-point smoothing
  python3 gpx_to_gc.py --install                           # copy to GoldenCheetah workouts dir

Output: ~/Documents/health_tracker/courses/<region>/filename.crs

GoldenCheetah CRS format:
  [COURSE HEADER]
  DESCRIPTION = Alpe d'Huez
  UNITS = METRIC
  [END COURSE HEADER]
  [COURSE DATA]
  0.000    0.0
  0.100    7.5
  0.200    8.1
  [END COURSE DATA]

  Each line: distance_km  gradient_%
"""

import argparse
import logging
import math  # noqa: F401  (retained for callers importing via this module)
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from geodesy import haversine as _haversine_p

GPX_DIR    = Path.home() / "Documents/GPX"
COURSE_DIR = Path(__file__).parent / "courses"
GC_DIR     = Path.home() / "Library/Application Support/GoldenCheetah"

DEFAULT_SMOOTH = 7  # rolling average window (points)
SAMPLE_DIST_M  = 50  # resample GPX to this spacing


# ── GPX parser ────────────────────────────────────────────────────────────────

def parse_gpx(path: Path) -> list[dict]:
    """
    Return list of {lat, lon, ele, dist_m} from a GPX file.
    dist_m is cumulative distance from start.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Handle GPX namespace
    ns = ""
    m = re.match(r'\{(.+?)\}', root.tag)
    if m:
        ns = m.group(1)

    def tag(name):
        return f"{{{ns}}}{name}" if ns else name

    filename = path.name
    last_valid_ele = 0.0
    # v3.6.0-fix28 L-7: distinguish first-point vs subsequent warns. When
    # idx==0 the anchor fallback is literally 0.0m — any CRS built on top
    # will have an offset elevation series; the more urgent warning helps
    # callers spot this rather than chasing ghost climbs later.
    first_point_anchored_to_zero = False

    def _parse_ele(ele_el, idx: int) -> float:
        """Parse <ele>; warn on missing/empty; fall back to last valid ele."""
        nonlocal last_valid_ele, first_point_anchored_to_zero
        if ele_el is None:
            if idx == 0:
                first_point_anchored_to_zero = True
                logging.warning(
                    "GPX %s: first track point has no <ele>; anchoring "
                    "elevation series to 0.0m (course elevations may be offset)",
                    filename,
                )
            else:
                logging.warning("GPX missing <ele> at track point %d of %s", idx, filename)
            return last_valid_ele
        try:
            ele = float(ele_el.text) if ele_el.text is not None else None
        except (TypeError, ValueError):
            ele = None
        if ele is None:
            if idx == 0:
                first_point_anchored_to_zero = True
                logging.warning(
                    "GPX %s: first track point has empty/invalid <ele>; "
                    "anchoring elevation series to 0.0m (course elevations "
                    "may be offset)",
                    filename,
                )
            else:
                logging.warning("GPX empty/invalid <ele> at track point %d of %s", idx, filename)
            return last_valid_ele
        last_valid_ele = ele
        return ele

    points = []
    # Try tracks first, then routes
    idx = 0
    for trkseg in root.iter(tag("trkseg")):
        for pt in trkseg.iter(tag("trkpt")):
            lat = float(pt.get("lat"))
            lon = float(pt.get("lon"))
            ele = _parse_ele(pt.find(tag("ele")), idx)
            points.append({"lat": lat, "lon": lon, "ele": ele})
            idx += 1

    if not points:
        idx = 0
        for pt in root.iter(tag("rtept")):
            lat = float(pt.get("lat"))
            lon = float(pt.get("lon"))
            ele = _parse_ele(pt.find(tag("ele")), idx)
            points.append({"lat": lat, "lon": lon, "ele": ele})
            idx += 1

    if not points:
        return []

    # Calculate cumulative distance using Haversine
    for i, p in enumerate(points):
        if i == 0:
            p["dist_m"] = 0.0
        else:
            p["dist_m"] = points[i - 1]["dist_m"] + _haversine_p(
                (points[i - 1]["lat"], points[i - 1]["lon"]),
                (p["lat"], p["lon"]),
            )
    return points


# ── Elevation smoothing ──────────────────────────────────────────────────────

def smooth_elevation(points: list[dict], window: int = DEFAULT_SMOOTH) -> list[dict]:
    """Apply rolling average to elevation to remove GPS noise."""
    if window < 2 or len(points) < window:
        return points
    half = window // 2
    smoothed = []
    for i, p in enumerate(points):
        lo = max(0, i - half)
        hi = min(len(points), i + half + 1)
        avg_ele = sum(pt["ele"] for pt in points[lo:hi]) / (hi - lo)
        smoothed.append({**p, "ele": avg_ele})
    return smoothed


# ── Resample to even spacing ─────────────────────────────────────────────────

def resample(points: list[dict], spacing_m: float = SAMPLE_DIST_M) -> list[dict]:
    """Resample points to even distance intervals via linear interpolation."""
    if len(points) < 2:
        return points

    total_dist = points[-1]["dist_m"]
    resampled  = []
    j = 0  # index into original points

    dist = 0.0
    while dist <= total_dist:
        # advance j to bracket current dist. Use <= so that when the target
        # dist lands exactly on points[j+1].dist_m we keep advancing into the
        # next segment (the inclusive compare avoids a double-use of the same
        # segment and the tiny interpolation drift it causes at exact hits).
        while j < len(points) - 1 and points[j + 1]["dist_m"] <= dist:
            j += 1
        if j >= len(points) - 1:
            resampled.append({**points[-1], "dist_m": dist})
            break

        # linear interpolation
        p0, p1 = points[j], points[j + 1]
        seg_len = p1["dist_m"] - p0["dist_m"]
        if seg_len > 0:
            frac = (dist - p0["dist_m"]) / seg_len
        else:
            frac = 0.0
        ele = p0["ele"] + frac * (p1["ele"] - p0["ele"])
        lat = p0["lat"] + frac * (p1["lat"] - p0["lat"])
        lon = p0["lon"] + frac * (p1["lon"] - p0["lon"])
        resampled.append({"lat": lat, "lon": lon, "ele": ele, "dist_m": dist})
        dist += spacing_m

    return resampled


# ── Gradient calculation ──────────────────────────────────────────────────────

def compute_gradients(points: list[dict]) -> list[tuple[float, float]]:
    """
    Return list of (distance_km, gradient_percent) for CRS file.
    Gradient is clamped to ±25% to avoid absurd values from GPS artifacts.
    """
    result = [(0.0, 0.0)]
    for i in range(1, len(points)):
        dist_km = points[i]["dist_m"] / 1000.0
        d_ele   = points[i]["ele"] - points[i - 1]["ele"]
        d_dist  = points[i]["dist_m"] - points[i - 1]["dist_m"]
        if d_dist > 0:
            grad = (d_ele / d_dist) * 100.0
            grad = max(-25.0, min(25.0, grad))  # clamp
        else:
            grad = 0.0
        result.append((dist_km, round(grad, 1)))
    return result


# ── CRS writer ────────────────────────────────────────────────────────────────

def write_crs(gradients: list[tuple[float, float]],
              name: str, dest: Path,
              total_climb: float, total_dist_km: float) -> None:
    """Write a GoldenCheetah .crs course file.

    GoldenCheetah's ErgFile.cpp parser accumulates each line's distance
    value (rdist += distance), so distances must be DELTA segment lengths,
    not cumulative positions.
    """
    lines = [
        "[COURSE HEADER]",
        f"DESCRIPTION = {name} ({total_dist_km:.1f}km, {total_climb:.0f}m climb)",
        f"FILE NAME = {dest.name}",
        "UNITS = METRIC",
        "[END COURSE HEADER]",
        "[COURSE DATA]",
        "DISTANCE\tGRADE\tWIND",
    ]
    prev_dist = 0.0
    for dist_km, grad in gradients:
        delta = dist_km - prev_dist
        prev_dist = dist_km
        lines.append(f"{delta:.3f}\t{grad:.1f}\t0")
    lines.append("[END COURSE DATA]")

    dest.write_text("\n".join(lines), encoding="utf-8")


# ── Stats ─────────────────────────────────────────────────────────────────────

def climb_stats(points: list[dict]) -> dict:
    """Calculate total ascent, descent, distance, avg/max gradient."""
    ascent = descent = 0.0
    max_grad = 0.0
    for i in range(1, len(points)):
        d_ele  = points[i]["ele"] - points[i - 1]["ele"]
        d_dist = points[i]["dist_m"] - points[i - 1]["dist_m"]
        if d_ele > 0:
            ascent += d_ele
        else:
            descent += abs(d_ele)
        if d_dist > 0:
            grad = abs(d_ele / d_dist) * 100
            max_grad = max(max_grad, grad)
    total_km = points[-1]["dist_m"] / 1000 if points else 0
    avg_grad = (ascent / (total_km * 10)) if total_km > 0 else 0
    return {
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "distance_km": round(total_km, 1),
        "avg_gradient": round(avg_grad, 1),
        "max_gradient": round(max_grad, 1),
        "start_ele": round(points[0]["ele"]) if points else 0,
        "summit_ele": round(max(p["ele"] for p in points)) if points else 0,
    }


# ── Batch convert ─────────────────────────────────────────────────────────────

def convert_gpx(gpx_path: Path, out_dir: Path, smooth_window: int) -> dict | None:
    """Convert a single GPX file to CRS. Returns stats dict or None on failure."""
    points = parse_gpx(gpx_path)
    if len(points) < 5:
        return None

    points   = resample(points, SAMPLE_DIST_M)
    points   = smooth_elevation(points, smooth_window)
    grads    = compute_gradients(points)
    stats    = climb_stats(points)
    name     = gpx_path.stem.replace("_", " ").replace("-", " ").title()
    crs_name = gpx_path.stem + ".crs"

    # Keep folder structure (region subfolder)
    rel = gpx_path.parent.name
    dest_dir = out_dir / rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / crs_name

    write_crs(grads, name, dest, stats["ascent_m"], stats["distance_km"])

    return {
        "name": name,
        "file": str(dest),
        "region": rel,
        **stats,
    }


def main():
    parser = argparse.ArgumentParser(description="Convert GPX → GoldenCheetah CRS courses")
    parser.add_argument("--input",   default=str(GPX_DIR),
                        help="GPX file or directory to convert")
    parser.add_argument("--out",     default=str(COURSE_DIR),
                        help="Output directory for CRS files")
    parser.add_argument("--smooth",  type=int, default=DEFAULT_SMOOTH,
                        help=f"Elevation smoothing window (default: {DEFAULT_SMOOTH})")
    parser.add_argument("--install", action="store_true",
                        help="Copy CRS files to GoldenCheetah workout folder")
    args = parser.parse_args()

    in_path  = Path(args.input)
    out_dir  = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if in_path.is_file():
        gpx_files = [in_path]
    else:
        gpx_files = sorted(in_path.rglob("*.gpx"))

    if not gpx_files:
        print(f"No GPX files found in {in_path}")
        return

    print(f"Converting {len(gpx_files)} GPX files → CRS courses (smooth={args.smooth})\n")

    results = []
    ok = fail = 0
    for gpx in gpx_files:
        try:
            stats = convert_gpx(gpx, out_dir, args.smooth)
        except Exception as e:
            # One bad file must not halt the batch. Log and carry on.
            logging.warning("convert_gpx failed for %s: %s", gpx.name, e)
            stats = None
        if stats:
            ok += 1
            results.append(stats)
            print(f"  ✓  {stats['region']}/{gpx.stem}.crs  "
                  f"({stats['distance_km']}km, {stats['ascent_m']}m↑, "
                  f"avg {stats['avg_gradient']}%, max {stats['max_gradient']}%)")
        else:
            fail += 1
            print(f"  ✗  {gpx.name} (too few points or parse error)")

    # Summary table
    if results:
        results.sort(key=lambda r: -r["ascent_m"])
        print(f"\n{'─'*90}")
        print(f"{'Name':<35} {'Region':<12} {'Dist':>6} {'Climb':>6} {'Avg%':>5} {'Max%':>5} {'Summit':>7}")
        print(f"{'─'*90}")
        for r in results:
            print(f"{r['name'][:34]:<35} {r['region']:<12} "
                  f"{r['distance_km']:>5.1f}km {r['ascent_m']:>5}m "
                  f"{r['avg_gradient']:>4.1f}% {r['max_gradient']:>4.1f}% "
                  f"{r['summit_ele']:>6}m")

    print(f"\n{ok} converted, {fail} failed")
    print(f"CRS files: {out_dir}")

    if args.install:
        gc_dirs = list(GC_DIR.iterdir()) if GC_DIR.exists() else []
        athlete_dirs = [d / "workouts" for d in gc_dirs if d.is_dir() and not d.name.startswith(".")]
        if not athlete_dirs:
            print(f"\n⚠  GoldenCheetah not found at {GC_DIR}")
        else:
            for ad in athlete_dirs:
                ad.mkdir(exist_ok=True)
                for crs in out_dir.rglob("*.crs"):
                    shutil.copy(crs, ad / crs.name)
                print(f"\n✓  Installed to {ad}")

    print("\nIn GoldenCheetah:")
    print("  1. Train view → scan for workouts")
    print("  2. Select a course → Simulation (slope) mode")
    print("  3. Tacx Neo 2T adjusts gradient automatically")


if __name__ == "__main__":
    main()
