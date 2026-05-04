#!/usr/bin/env python3
"""Generate profile JSONs + profiles_indexed.json + routes.json for the route preview UI.

Reads all CRS files under courses/ and generates:
- profiles/<world>__<route_slug>.json  (individual route preview data)
- profiles_indexed.json                (compact index of all profiles)
- routes.json                          (virtual-world route catalog used by app.py)

Format matches what app.py's _load_route_detail() expects.

`avg_grade` is the *net signed* average gradient (negative for net descents,
zero for loops, positive for net climbs):

    avg_grade_pct = (e_end - e_start) / (distance_m) * 100

`max_grade` is the maximum positive segment gradient (a climb-focused metric).
`avg_abs_grade` (profiles only) is the mean of |segment_grade| — useful for
overall route "lumpiness" but not a directional signal.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
COURSES = ROOT / "courses"
PROFILES = ROOT / "profiles"
INDEX = ROOT / "profiles_indexed.json"
# Preview writer: emit to /tmp to avoid clobbering the canonical flat-list
# routes.json (622 entries w/ archetype, difficulty_score, surface_mix, tags).
# The format emitted here is the legacy nested `{version, worlds:[...]}` schema
# and MUST NOT replace the production routes.json without manual review.
ROUTES_JSON = Path("/tmp/routes_preview.json")

# Metadata for the virtual worlds emitted into routes.json. Kept in sync with
# the procedural generator (generate_procedural_routes.py).
VIRTUAL_WORLDS = [
    {
        "name": "Blue Ridge",
        "slug": "blue_ridge",
        "description": "Rolling hills and mixed terrain",
    },
    {
        "name": "Iron Pass",
        "slug": "iron_pass",
        "description": "Mountain climbs and summit finishes",
    },
    {
        "name": "Desert Loop",
        "slug": "desert_loop",
        "description": "Flat TT courses and light rollers",
    },
]
ROUTES_NOTE = (
    "Procedurally generated routes. Apache-2.0 licensed. No third-party route data."
)


def parse_crs(path: Path):
    segs, grades = [], []
    desc = ""
    in_data = False
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("DESCRIPTION"):
                _, _, v = s.partition("=")
                desc = v.strip()
            if "[COURSE DATA]" in s:
                in_data = True
                continue
            if not in_data or s.startswith("[") or s.startswith("DISTANCE"):
                continue
            parts = s.split()
            if len(parts) < 2:
                continue
            try:
                segs.append(float(parts[0]))
                grades.append(float(parts[1]))
            except ValueError:
                pass
    return segs, grades, desc


def build_profile(segs, grades, max_points=200):
    """Build elevation profile [{d, e, g}, ...] from segment data."""
    points = []
    cum_d = 0.0
    cum_e = 0.0
    for d, g in zip(segs, grades):
        cum_d += d
        cum_e += d * 1000 * g / 100  # elevation change in meters
        points.append({"d": round(cum_d, 3), "e": round(cum_e, 1), "g": round(g, 1)})
    if len(points) > max_points:
        step = max(1, len(points) // max_points)
        points = points[::step] + [points[-1]]
    return points


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s


def main():
    PROFILES.mkdir(exist_ok=True)
    # Clear any stale files
    for old in PROFILES.glob("*.json"):
        old.unlink()

    index = {}
    # routes.json catalog: world_slug -> list of route dicts
    routes_by_world: dict[str, list[dict]] = {w["slug"]: [] for w in VIRTUAL_WORLDS}
    count = 0

    # Walk all CRS files
    for crs_path in COURSES.rglob("*.crs"):
        rel = crs_path.relative_to(COURSES)
        parts = rel.parts
        # e.g., parts = ("virtual", "blue_ridge", "blue-ridge__morning-loop-1.crs")
        # or    parts = ("alps", "Climb Alps - Aprica.crs")

        stem = crs_path.stem  # filename without .crs

        # Determine world key and route slug
        if parts[0] == "virtual" and len(parts) >= 3:
            world = parts[1]  # "blue_ridge", "iron_pass", "desert_loop"
            # Route slug = the part after "<world>__"
            if "__" in stem:
                slug = stem.split("__", 1)[1]
            else:
                slug = slugify(stem)
            is_virtual = True
        else:
            world = parts[0]  # "alps", "pyrenees", etc.
            slug = slugify(stem)
            is_virtual = False

        try:
            segs, grades, desc = parse_crs(crs_path)
        except Exception:
            continue
        if not segs:
            continue

        total_km = sum(segs)
        elev_gain = sum(d * 1000 * g / 100 for d, g in zip(segs, grades) if g > 0)
        max_g = max(grades) if grades else 0
        avg_abs_g = sum(abs(g) for g in grades) / len(grades) if grades else 0

        profile = build_profile(segs, grades)

        # Net signed average grade = (elevation_end - elevation_start) / distance_total
        # Measured in %. Negative for net descents, ~0 for loops, positive for net climbs.
        if total_km > 0 and len(profile) >= 2:
            net_elev_m = profile[-1]["e"] - profile[0]["e"]
            avg_grade = round((net_elev_m / (total_km * 1000)) * 100, 1)
        else:
            avg_grade = 0.0

        # Extract route name from description or generate
        # e.g., "Domestique: Blue Ridge - Morning Loop 42 (15.5km)"
        # (also matches legacy "ChickenCycling: ..." header for older files)
        m = re.search(r": .+? - (.+?) \(", desc)
        name = m.group(1) if m else stem.replace("-", " ").replace("_", " ").title()

        # Individual profile file
        profile_json = {
            "world": world,
            "slug": slug,
            "name": name,
            "distance_km": round(total_km, 2),
            "elev_gain_m": round(elev_gain),
            "max_grade": round(max_g, 1),
            "avg_grade": avg_grade,
            "avg_abs_grade": round(avg_abs_g, 2),
            "profile": profile,
        }

        filename = f"{world}__{slug}.json"
        (PROFILES / filename).write_text(json.dumps(profile_json, separators=(",", ":")))

        # Index entry (compact)
        index_key = f"{world}/{slug}"
        index[index_key] = {
            "name": name,
            "world": world,
            "distance_km": round(total_km, 2),
            "elev_gain_m": round(elev_gain),
            "max_grade": round(max_g, 1),
            "avg_grade": avg_grade,
            "profile": profile[:50] if len(profile) > 50 else profile,  # compact preview
        }

        # routes.json entry — virtual worlds only
        if is_virtual and world in routes_by_world:
            routes_by_world[world].append({
                "name": name,
                "file": crs_path.name,
                "distance_km": round(total_km, 2),
                "climb_m": round(elev_gain),
                "max_grade": round(max_g, 1),
                "avg_grade": avg_grade,
                "path": "/".join(parts),
            })

        count += 1

    # Write index
    INDEX.write_text(json.dumps(index, separators=(",", ":")))

    # Write routes.json (virtual worlds catalog)
    worlds_out = []
    for w in VIRTUAL_WORLDS:
        rs = sorted(routes_by_world[w["slug"]], key=lambda r: r["name"])
        worlds_out.append({
            "name": w["name"],
            "slug": w["slug"],
            "description": w["description"],
            "route_count": len(rs),
            "routes": rs,
        })
    routes_doc = {
        "version": 2,
        "note": ROUTES_NOTE,
        "worlds": worlds_out,
        "total_routes": sum(len(w["routes"]) for w in worlds_out),
    }
    ROUTES_JSON.write_text(json.dumps(routes_doc, indent=2) + "\n")
    print("WARNING: wrote preview to /tmp; do NOT copy to prod routes.json without review")

    print(f"Generated {count} profile files in profiles/")
    print(f"Index: profiles_indexed.json ({INDEX.stat().st_size // 1024} KB)")
    print(
        f"routes.json: {routes_doc['total_routes']} virtual routes "
        f"({ROUTES_JSON.stat().st_size // 1024} KB)"
    )

    # Per-world counts
    per_world = {}
    for k in index.keys():
        w = k.split("/")[0]
        per_world[w] = per_world.get(w, 0) + 1
    print("\nPer-world count:")
    for w, c in sorted(per_world.items()):
        print(f"  {w}: {c}")


if __name__ == "__main__":
    main()
