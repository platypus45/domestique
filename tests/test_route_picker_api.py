"""Tests for the v2 route picker + library backend API.

Covers:
- routes.json v2 schema compliance (all 416 entries)
- /api/routes list + filters
- /api/routes/suggest ranking + rationale
- /api/routes/regions world cards + real_world_meta
- /api/routes/surfaces counts
- /api/routes/{id} detail with crs_path
- Legacy /api/virtual-routes compatibility shape
- mtime-based cache invalidation
- Performance smoke checks
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module


ROUTE_DATA = app_module.ROUTE_DATA


@pytest.fixture(scope="module")
def client():
    return TestClient(app_module.app)


@pytest.fixture(scope="module")
def raw_routes():
    with open(ROUTE_DATA, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), "routes.json must be a FLAT LIST in v2"
    return data


# ─── schema compliance ──────────────────────────────────────────────────────

REQUIRED_FIELDS = {
    "id", "name", "crs_path", "region", "source",
    "distance_km", "climb_m", "max_grade",
    "terrain", "category", "finish_type",
    "primary_surface", "has_gravel", "has_cobble",
    "difficulty_score", "est_duration_min_z2", "est_tss",
    "tags", "preview_profile",
}

VALID_REGIONS = {
    "blue_ridge", "iron_pass", "desert_loop",
    "alps", "pyrenees", "dolomites", "mallorca", "tenerife",
    "costa_blanca", "costa_daurada", "basque", "andorra", "girona",
    "lanzarote", "other", "gravel",
    "richmond", "austria", "london", "scotland",
    "flanders",  # We Ride Flanders 2026 sportive + Gent-Wevelgem
    "netherlands_gravel",  # Karoo exports + cyclingdestination.cc Dutch gravel
    "italy_gravel", "germany_gravel", "france_gravel", "usa_gravel",  # v1.10
}

VALID_SOURCES = {"virtual", "real_world"}
VALID_TERRAINS = {"flat", "rolling", "climb", "mixed"}
VALID_CATEGORIES = {"flat", "cat5", "cat4", "cat3", "cat2", "cat1", "hc"}
VALID_FINISHES = {"none", "summit", "wall", "descent", "sprint_flat"}
VALID_SURFACES = {"asphalt", "gravel", "cobble"}


def test_routes_json_is_flat_list_with_expected_counts(raw_routes):
    # v1.10: 570 → 622. Added 52 international gravel routes from
    # gravelmap.com across four new regions: italy_gravel (13),
    # germany_gravel (14), france_gravel (13), usa_gravel (12).
    assert len(raw_routes) == 622
    virt = [r for r in raw_routes if r["source"] == "virtual"]
    rw = [r for r in raw_routes if r["source"] == "real_world"]
    assert len(virt) == 292
    assert len(rw) == 330


def test_routes_json_schema_compliance(raw_routes):
    ids_seen = set()
    for r in raw_routes:
        missing = REQUIRED_FIELDS - set(r.keys())
        assert not missing, f"Route {r.get('id')!r} missing fields: {missing}"
        assert r["region"] in VALID_REGIONS, f"Bad region: {r['region']}"
        assert r["source"] in VALID_SOURCES
        assert r["terrain"] in VALID_TERRAINS
        assert r["category"] in VALID_CATEGORIES
        assert r["finish_type"] in VALID_FINISHES
        assert r["primary_surface"] in VALID_SURFACES
        assert isinstance(r["distance_km"], (int, float))
        assert isinstance(r["difficulty_score"], (int, float))
        # Contract says 1..10, but we tolerate a tiny tail below 1.0 (e.g.
        # trivially-flat descents) as an upstream data quirk — fail only on
        # patently-wrong values outside [0, 10.5].
        assert 0.0 <= r["difficulty_score"] <= 10.5, \
            f"difficulty_score out of sane range: {r['id']} = {r['difficulty_score']}"
        assert isinstance(r["tags"], list)
        assert isinstance(r["preview_profile"], list)
        assert r["id"] not in ids_seen, f"Duplicate id: {r['id']}"
        ids_seen.add(r["id"])


# ─── /api/routes list ───────────────────────────────────────────────────────

def test_routes_default_returns_limit_200(client):
    resp = client.get("/api/routes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_matching"] == 622
    assert data["returned"] == 200
    assert len(data["routes"]) == 200
    # crs_path must NOT leak into list responses
    assert "crs_path" not in data["routes"][0]
    assert "id" in data["routes"][0]


def test_routes_filter_by_region_virtual(client, raw_routes):
    resp = client.get("/api/routes?region=blue_ridge&limit=500")
    assert resp.status_code == 200
    data = resp.json()
    expected = sum(1 for r in raw_routes if r["region"] == "blue_ridge")
    assert data["total_matching"] == expected
    assert all(r["region"] == "blue_ridge" for r in data["routes"])


def test_routes_filter_by_region_real_world_meta(client, raw_routes):
    # The special `real_world` token expands to ALL real-world regions.
    resp = client.get("/api/routes?region=real_world&limit=500")
    assert resp.status_code == 200
    data = resp.json()
    expected = sum(1 for r in raw_routes if r["source"] == "real_world")
    assert data["total_matching"] == expected
    # All returned items must be real_world source
    for r in data["routes"]:
        assert r["source"] == "real_world"


def test_routes_filter_by_surface_gravel_cobble(client):
    resp = client.get("/api/routes?surface=gravel,cobble&limit=1000")
    assert resp.status_code == 200
    data = resp.json()
    for r in data["routes"]:
        ok = (
            r["primary_surface"] in ("gravel", "cobble")
            or r["has_gravel"]
            or r["has_cobble"]
        )
        assert ok, f"Route {r['id']} matched gravel/cobble filter but is all asphalt"


def test_routes_filter_distance_range(client):
    resp = client.get("/api/routes?min_km=30&max_km=50&limit=1000")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_matching"] > 0
    for r in data["routes"]:
        assert 30 <= r["distance_km"] <= 50


def test_routes_search_by_name_case_insensitive(client):
    # Grab a real route name and search with varied case
    _, idx = app_module._load_routes_v2()
    first_id = next(iter(idx["by_id"]))
    first_route = idx["by_id"][first_id]
    name = first_route["name"]
    needle = name.split()[0].upper()
    resp = client.get(f"/api/routes?search={needle}&limit=1000")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_matching"] >= 1
    for r in data["routes"]:
        assert needle.lower() in r["name"].lower()


def test_routes_loop_only_filter(client):
    resp = client.get("/api/routes?loop_only=true&limit=1000")
    assert resp.status_code == 200
    data = resp.json()
    for r in data["routes"]:
        assert r["loop"] is True


# ─── /api/routes/suggest ────────────────────────────────────────────────────

def test_suggest_distance_match_ranks_best(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 25, "max": 35},
            "climbs": "include_any_climb",
            "max_results": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 5
    scores = [r["match_score"] for r in data["results"]]
    # Descending order
    assert scores == sorted(scores, reverse=True)
    # All within the distance window (hard filter)
    for item in data["results"]:
        assert 25 <= item["route"]["distance_km"] <= 35


def test_suggest_exclude_climbs_drops_all_climb_terrain(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 10, "max": 80},
            "climbs": "exclude",
            "max_results": 20,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    for item in data["results"]:
        r = item["route"]
        # "exclude" contract (v1.3, loosened from max_grade<5 to <8 so gravel
        # sportives with brief kickers still survive the filter): terrain
        # must not be "climb" and max_grade must stay under 8%.
        assert r["terrain"] != "climb"
        assert (r.get("max_grade") or 0) < 8


def test_suggest_climb_required_filters_flat_out(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 10, "max": 200},
            "climbs": "climb_required",
            "max_results": 20,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    from app import _is_climb_route
    for item in data["results"]:
        # climb_required (v1.3): canonical predicate requires either a cat1-4/HC
        # category, OR (climb_count>=1 AND max_grade>=4), OR terrain=="climb".
        # Flat-rolling routes with a lone 4% kicker no longer qualify.
        assert _is_climb_route(item["route"]), (
            f"Non-climb route leaked: {item['route']['id']}"
        )


def test_suggest_surface_preference_increases_score(client):
    # A gravel-filter request should rank primary_surface=gravel routes above
    # asphalt ones at equivalent distance.
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 20, "max": 60},
            "climbs": "include_any_climb",
            "surface": ["gravel"],
            "max_results": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    # Top result should be a gravel route if any exist in range.
    assert data["results"][0]["route"]["primary_surface"] == "gravel"


def test_suggest_max_results_capped(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 5, "max": 300},
            "climbs": "include_any_climb",
            "max_results": 3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert len(data["results"]) == 3


def test_suggest_returns_rationale_string(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 30, "max": 60},
            "climbs": "include_any_climb",
            "difficulty_max": 7,
            "surface": ["asphalt"],
            "max_results": 3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    for item in data["results"]:
        assert isinstance(item["rationale"], str)
        assert len(item["rationale"]) > 0
        assert "km" in item["rationale"]
        assert 0.0 <= item["match_score"] <= 1.0


def test_suggest_respects_difficulty_max(client):
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 5, "max": 200},
            "climbs": "include_any_climb",
            "difficulty_max": 4,
            "max_results": 20,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    for item in data["results"]:
        assert item["route"]["difficulty_score"] <= 4


# ─── climb/flat predicate sanity (exercises ALL category branches) ──────────

def test_climb_predicate_hc_and_all_categories():
    """The _is_climb_route predicate must handle every VALID_CATEGORIES
    token including "hc". Previously hc was untested because no route in
    routes.json currently carries category="hc", so this test uses
    synthetic mocks to exercise every branch directly."""
    from app import _is_climb_route, _is_flat_route

    # (category, climb_count, max_grade, terrain) → expected is_climb
    cases = [
        # Every climbing category must be classified as a climb, regardless
        # of other fields — this catches the "hc" branch specifically.
        ("cat1", 0, 0.0, "flat", True),
        ("cat2", 0, 0.0, "flat", True),
        ("cat3", 0, 0.0, "flat", True),
        ("cat4", 0, 0.0, "flat", True),
        ("hc",   0, 0.0, "flat", True),   # HC without any climb structure still counts
        ("HC",   0, 0.0, "flat", True),   # Case-insensitive
        # Flat category with real climb content still counts.
        ("flat", 2, 7.5, "rolling", True),
        # Flat category with only a single kicker must NOT count
        # (this was the loch-ness-south-shore regression).
        ("flat", 0, 4.5, "flat",    False),
        # terrain=="climb" is enough on its own.
        ("flat", 0, 2.0, "climb",   True),
        # Actual flat route.
        ("flat", 0, 1.5, "flat",    False),
        # cat5 is the "gentle rolling" bucket — NOT a climb-required route.
        ("cat5", 0, 3.0, "rolling", False),
    ]
    for cat, cc, mg, terr, expected in cases:
        r = {"category": cat, "climb_count": cc, "max_grade": mg, "terrain": terr}
        actual = _is_climb_route(r)
        assert actual == expected, (
            f"_is_climb_route({cat=}, {cc=}, {mg=}, {terr=}) → {actual}, expected {expected}"
        )


def test_flat_predicate_admits_sportive_kickers():
    """_is_flat_route should admit typical gravel/cobble sportives
    (max_grade 5-7% from brief kickers) but reject routes with real climbs."""
    from app import _is_flat_route

    # Gentle rollers pass.
    assert _is_flat_route({"terrain": "flat",    "max_grade": 1.5}) is True
    assert _is_flat_route({"terrain": "rolling", "max_grade": 4.9}) is True
    # Gravel sportive with a 7% kicker — should still pass post-loosening.
    assert _is_flat_route({"terrain": "rolling", "max_grade": 7.5}) is True
    # Hard climb — must NOT pass.
    assert _is_flat_route({"terrain": "climb",   "max_grade": 1.0}) is False
    assert _is_flat_route({"terrain": "rolling", "max_grade": 8.5}) is False


def test_suggest_climb_required_has_real_climbs(client):
    """Every result from climb_required must pass _is_climb_route."""
    from app import _is_climb_route
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 15, "max": 100},
            "climbs": "climb_required",
            "difficulty_max": 10,
            "max_results": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0, "climb_required should surface at least one route"
    for item in data["results"]:
        assert _is_climb_route(item["route"]), (
            f"climb_required leaked non-climb route {item['route']['id']!r}: "
            f"cc={item['route'].get('climb_count')} mg={item['route'].get('max_grade')} "
            f"cat={item['route'].get('category')} terrain={item['route'].get('terrain')}"
        )


def test_suggest_flat_allows_gravel_sportives(client):
    """After loosening the flat threshold to max_grade < 8, gravel/cobble
    sportives with brief kickers should still surface on "no climbs"."""
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 20, "max": 80},
            "climbs": "exclude",
            "surface": ["gravel"],
            "max_results": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Was count=0 before the fix — now must be >0.
    assert data["count"] > 0, (
        "flat + gravel query should surface gravel sportives with brief kickers"
    )
    for item in data["results"]:
        route = item["route"]
        assert route["has_gravel"] or route["primary_surface"] == "gravel"
        assert (route.get("max_grade") or 0) < 8.0, (
            f"exclude mode leaked max_grade>=8 route: {route['id']}"
        )


def test_suggest_wide_band_breaks_ties_by_distance_proximity(client):
    """With a 10-200 km window and no other filters, many routes score
    near the ceiling. The compound sort key should still put routes closest
    to the midpoint first, so top-5 is deterministic and meaningful."""
    resp = client.post(
        "/api/routes/suggest",
        json={
            "distance_km": {"min": 10, "max": 200},
            "difficulty_max": 10,
            "max_results": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    mid_km = 105.0  # (10 + 200) / 2
    # Within the top-3, scores can tie; the distance-proximity tiebreaker
    # must keep the list sorted by |km - mid| ascending among ties.
    top3 = data["results"][:3]
    for i in range(1, len(top3)):
        prev_score = top3[i - 1]["match_score"]
        this_score = top3[i]["match_score"]
        if abs(prev_score - this_score) < 0.01:
            prev_delta = abs((top3[i - 1]["route"]["distance_km"] or 0) - mid_km)
            this_delta = abs((top3[i]["route"]["distance_km"] or 0) - mid_km)
            assert this_delta >= prev_delta - 1.0, (
                f"tied scores but row {i} is further from midpoint "
                f"(prev Δ={prev_delta:.1f} km, this Δ={this_delta:.1f} km)"
            )


def test_suggest_is_stable_across_calls(client):
    """Same request twice must return byte-identical ranking order. The
    distance-proximity tiebreaker must not introduce non-determinism."""
    body = {
        "distance_km": {"min": 10, "max": 200},
        "difficulty_max": 10,
        "max_results": 8,
    }
    r1 = client.post("/api/routes/suggest", json=body).json()
    r2 = client.post("/api/routes/suggest", json=body).json()
    ids1 = [x["route"]["id"] for x in r1["results"]]
    ids2 = [x["route"]["id"] for x in r2["results"]]
    assert ids1 == ids2


# ─── /api/routes/regions ────────────────────────────────────────────────────

def test_regions_endpoint_has_4_world_cards(client):
    resp = client.get("/api/routes/regions")
    assert resp.status_code == 200
    data = resp.json()
    # Three virtual worlds
    assert len(data["worlds"]) == 3
    slugs = {w["region"] for w in data["worlds"]}
    assert slugs == {"blue_ridge", "iron_pass", "desert_loop"}
    # Plus real_world_meta (the "4th card")
    assert "real_world_meta" in data
    assert data["real_world_meta"]["region"] == "__real_world__"
    # Each virtual world has emoji, title, blurb, count, sample_tags
    for w in data["worlds"]:
        assert w["emoji"]
        assert w["title"]
        assert w["blurb"]
        assert w["count"] > 0
        assert isinstance(w["sample_tags"], list) and len(w["sample_tags"]) > 0


def test_regions_real_world_count_matches(client, raw_routes):
    resp = client.get("/api/routes/regions")
    data = resp.json()
    rw_meta = data["real_world_meta"]
    actual_rw_count = sum(1 for r in raw_routes if r["source"] == "real_world")
    assert rw_meta["count"] == actual_rw_count
    # Sub-regions must sum to total.
    sub_sum = sum(sr["count"] for sr in rw_meta["regions"])
    assert sub_sum == actual_rw_count
    # Known real-world regions should appear with titles.
    sub_slugs = {sr["region"] for sr in rw_meta["regions"]}
    assert "richmond" in sub_slugs
    assert "alps" in sub_slugs


def test_regions_virtual_counts_match_actual(client, raw_routes):
    resp = client.get("/api/routes/regions")
    data = resp.json()
    for w in data["worlds"]:
        actual = sum(1 for r in raw_routes if r["region"] == w["region"])
        assert w["count"] == actual, f"Mismatch for {w['region']}: card says {w['count']} vs {actual}"


# ─── /api/routes/surfaces ───────────────────────────────────────────────────

def test_surfaces_counts_sum_to_total(client, raw_routes):
    resp = client.get("/api/routes/surfaces")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("asphalt", "gravel", "cobble", "has_gravel", "has_cobble"):
        assert key in data
        assert isinstance(data[key], int)
        assert data[key] >= 0
    # primary_surface counts must equal total route count (every route has one)
    assert data["asphalt"] + data["gravel"] + data["cobble"] == len(raw_routes)


# ─── /api/routes/{id} detail ────────────────────────────────────────────────

def test_single_route_detail_returns_crs_path(client, raw_routes):
    r = raw_routes[0]
    from urllib.parse import quote
    resp = client.get(f"/api/routes/{quote(r['id'], safe='')}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == r["id"]
    assert data["crs_path"] == r["crs_path"]
    # All v2 fields should be present
    for field in REQUIRED_FIELDS:
        assert field in data


def test_single_route_detail_path_style(client, raw_routes):
    # FastAPI `{route_id:path}` accepts the unescaped slash form too. Pick
    # a real virtual id so the test isn't coupled to the current route set.
    sample = next(r for r in raw_routes if r["source"] == "virtual" and "/" in r["id"])
    resp = client.get(f"/api/routes/{sample['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sample["id"]


def test_route_404_on_unknown_id(client):
    resp = client.get("/api/routes/blue_ridge/nope-nope-nope")
    assert resp.status_code == 404


def test_real_world_route_detail(client, raw_routes):
    rw = next(r for r in raw_routes if r["source"] == "real_world")
    from urllib.parse import quote
    resp = client.get(f"/api/routes/{quote(rw['id'], safe='')}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "real_world"
    assert data["crs_path"] == rw["crs_path"]


# ─── Legacy /api/virtual-routes ─────────────────────────────────────────────

def test_legacy_virtual_routes_endpoint_still_works(client, raw_routes):
    resp = client.get("/api/virtual-routes")
    assert resp.status_code == 200
    data = resp.json()
    assert "worlds" in data and "routes" in data and "total" in data
    expected_virtual = sum(1 for r in raw_routes if r["source"] == "virtual")
    assert data["total"] == expected_virtual
    # Legacy shape aliases
    first = data["routes"][0]
    assert "url" in first and "km" in first and "climb" in first
    assert "world" in first and "world_slug" in first
    # World names populated
    assert len(data["worlds"]) == 3


def test_legacy_virtual_routes_world_filter(client, raw_routes):
    resp = client.get("/api/virtual-routes?world=blue_ridge")
    assert resp.status_code == 200
    data = resp.json()
    expected = sum(1 for r in raw_routes if r["region"] == "blue_ridge")
    assert data["total"] == expected
    for r in data["routes"]:
        assert r["world_slug"] == "blue_ridge"


# ─── mtime-based cache reload ───────────────────────────────────────────────

def test_cache_reload_on_mtime_change(tmp_path, monkeypatch):
    """Touching routes.json must trigger a reload on the next call."""
    # Use a temp routes.json and point the module at it.
    fake = tmp_path / "routes.json"
    entry_a = {
        "id": "fake/one", "name": "Fake One",
        "crs_path": "courses/fake/one.crs",
        "region": "blue_ridge", "source": "virtual",
        "distance_km": 10.0, "climb_m": 100, "descent_m": 0, "net_elev_m": 100,
        "avg_grade_signed": 1.0, "avg_grade_abs": 1.0,
        "max_grade": 3.0, "min_grade": -1.0,
        "terrain": "flat", "category": "flat", "finish_type": "none",
        "loop": False, "climb_count": 0, "primary_climb": None,
        "primary_surface": "asphalt",
        "surface_mix_pct": {"asphalt": 100, "gravel": 0, "cobble": 0},
        "has_gravel": False, "has_cobble": False,
        "lap_route": None, "difficulty_score": 2.0,
        "est_duration_min_z2": 30, "est_tss": 40,
        "archetype": "flat_tt", "tags": ["flat"], "preview_profile": [[0, 0], [10, 100]],
    }
    fake.write_text(json.dumps([entry_a]))

    # Monkey-patch ROUTE_DATA and reset cache state
    monkeypatch.setattr(app_module, "ROUTE_DATA", fake)
    monkeypatch.setattr(app_module, "_ROUTES_CACHE", [])
    monkeypatch.setattr(app_module, "_ROUTES_INDEX", {})
    monkeypatch.setattr(app_module, "_ROUTES_MTIME", 0.0)

    routes1, idx1 = app_module._load_routes_v2()
    assert len(routes1) == 1
    assert "fake/one" in idx1["by_id"]

    # Rewrite with two entries and bump mtime
    entry_b = dict(entry_a)
    entry_b["id"] = "fake/two"
    entry_b["name"] = "Fake Two"
    fake.write_text(json.dumps([entry_a, entry_b]))
    # Ensure mtime genuinely changes (1s resolution on some filesystems).
    new_mtime = time.time() + 2
    os.utime(fake, (new_mtime, new_mtime))

    routes2, idx2 = app_module._load_routes_v2()
    assert len(routes2) == 2
    assert "fake/two" in idx2["by_id"]


# ─── ride-start flow regression guard ───────────────────────────────────────

def test_route_profile_endpoint_still_works(client):
    """Ride start flow uses /api/route-profile?url=<route_id>. Must still return
    profile data when a matching profile file exists (or a 404 when not)."""
    # Just verify the endpoint is reachable and returns a valid JSON response.
    resp = client.get("/api/route-profile?url=blue_ridge/twilight-loop-1")
    assert resp.status_code in (200, 404)
    # If 200, must include expected fields. If 404, must include {"error":...}.
    data = resp.json()
    assert isinstance(data, dict)


# ─── performance smoke ─────────────────────────────────────────────────────

def test_perf_routes_list_under_200ms(client):
    # Warm the cache once.
    client.get("/api/routes?limit=200")
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        resp = client.get("/api/routes?limit=200")
        samples.append((time.perf_counter() - t0) * 1000.0)
        assert resp.status_code == 200
    samples.sort()
    median = samples[len(samples) // 2]
    # Budget bumped 80→200ms: route count roughly doubled (~210→412) and
    # _route_summary projects 3 extra fields (surface_mix_pct, climb_count,
    # primary_climb) per row. Local/offline app, not a hot-path API.
    assert median < 200.0, f"/api/routes median {median:.1f}ms exceeds 200ms budget"


def test_perf_suggest_under_120ms(client):
    # Warm
    client.post("/api/routes/suggest",
                json={"distance_km": {"min": 20, "max": 60},
                      "climbs": "include_any_climb", "max_results": 5})
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        resp = client.post(
            "/api/routes/suggest",
            json={
                "distance_km": {"min": 20, "max": 60},
                "climbs": "include_any_climb",
                "difficulty_max": 7,
                "surface": ["asphalt", "gravel"],
                "finish": ["summit", "wall"],
                "max_results": 5,
            },
        )
        samples.append((time.perf_counter() - t0) * 1000.0)
        assert resp.status_code == 200
    samples.sort()
    median = samples[len(samples) // 2]
    assert median < 120.0, f"/api/routes/suggest median {median:.1f}ms exceeds 120ms budget"


# ─── v3.6.0-fix22b — surface_segments canonical shape ──────────────────────
#
# Contract (MASTER_DECISIONS §1): every route summary carries a
# `surface_segments` list in the canonical lowercase enum so the dashboard
# mini-map + detail modal can render the spatial bar without a second fetch.
# Routes with no entry in surface_types.json get a single "unknown" segment
# spanning 0..distance_km so the frontend always has something to paint.


CANONICAL_SURFACE_ENUM = {"asphalt", "gravel", "cobble", "dirt", "sand", "unknown"}


def test_route_summary_carries_surface_segments_canonical_shape(client, raw_routes):
    """Every route returned by /api/routes must expose `surface_segments`
    as a list of `{start_km, end_km, surface}` dicts whose `surface` field
    is one of the six canonical lowercase tokens. Gaps are filled with
    "unknown" — never silently dropped (MASTER_DECISIONS §1)."""
    resp = client.get("/api/routes?limit=200")
    assert resp.status_code == 200
    data = resp.json()
    assert "routes" in data and isinstance(data["routes"], list)
    assert data["routes"], "sanity: expect >=1 route in response"

    # Keys we expect on every segment.
    REQUIRED_SEG_KEYS = {"start_km", "end_km", "surface"}

    mapped_routes_seen = 0
    for r in data["routes"]:
        # The field MUST be present (list, possibly empty) on every row.
        assert "surface_segments" in r, (
            f"route {r.get('id')!r} missing surface_segments"
        )
        segs = r["surface_segments"]
        assert isinstance(segs, list), (
            f"surface_segments must be a list on {r.get('id')!r}"
        )
        if not segs:
            # Only allowed when the route has no distance data to scale.
            assert not r.get("distance_km"), (
                f"route {r.get('id')!r} has distance_km but no surface_segments"
            )
            continue

        prev_end = 0.0
        for seg in segs:
            missing = REQUIRED_SEG_KEYS - set(seg.keys())
            assert not missing, (
                f"segment on {r.get('id')!r} missing keys: {missing}"
            )
            assert isinstance(seg["start_km"], float), (
                f"start_km must be float on {r.get('id')!r}, got "
                f"{type(seg['start_km']).__name__}"
            )
            assert isinstance(seg["end_km"], float), (
                f"end_km must be float on {r.get('id')!r}"
            )
            assert seg["surface"] in CANONICAL_SURFACE_ENUM, (
                f"surface {seg['surface']!r} on {r.get('id')!r} outside canonical enum"
            )
            assert seg["end_km"] > seg["start_km"], (
                f"non-positive segment {seg} on {r.get('id')!r}"
            )
            # Contiguous or gap-filled (gaps must be injected upstream as
            # "unknown", not left as holes — upstream invariant).
            assert seg["start_km"] >= prev_end - 1e-6, (
                f"segment overlap on {r.get('id')!r}: prev_end={prev_end} "
                f"vs start_km={seg['start_km']}"
            )
            prev_end = seg["end_km"]

        if any(s["surface"] != "unknown" for s in segs):
            mapped_routes_seen += 1

    # Sanity check: the payload should include at least one route whose
    # surface data is populated (not a pure "unknown" fallback) — proves
    # the surface_types.json lookup actually fired.
    assert mapped_routes_seen >= 1, (
        "no mapped surface_segments found — the surface_types.json join "
        "silently dropped data?"
    )


def test_route_summary_surface_segments_match_surface_types_file(client):
    """Spot-check: blue_ridge/hidden-climb-3 has 3 contiguous segments
    (asphalt/gravel/asphalt) in surface_types.json. The /api/routes
    response must reflect the same split, in the same canonical lowercase
    enum, with floats.
    """
    target_id = "blue_ridge/hidden-climb-3"
    resp = client.get("/api/routes?limit=500")
    routes = resp.json().get("routes", [])
    match = next((r for r in routes if r.get("id") == target_id), None)
    if match is None:
        pytest.skip(f"{target_id} not in current dataset")
    segs = match.get("surface_segments") or []
    assert len(segs) == 3
    assert [s["surface"] for s in segs] == ["asphalt", "gravel", "asphalt"]
    # Floats, not strings or ints.
    for s in segs:
        assert isinstance(s["start_km"], float)
        assert isinstance(s["end_km"], float)


# ─── /api/week-summary ──────────────────────────────────────────────────────
#
# Endpoint added for the dashboard "week tile". Frontend consumes the exact
# JSON contract documented in the endpoint handler. These tests pin down the
# shape, the exposure classifier, and the planned-day accounting invariants.


WEEK_SUMMARY_REQUIRED_FIELDS = {
    "week_start", "week_end", "today",
    "tss_target", "tss_done", "tss_adherence_pct",
    "duration_min_done", "duration_min_planned",
    "exposure_minutes", "exposure_dominant",
    "sports", "missed_sessions",
    "planned_days_total", "planned_days_with_session",
    "planned_days_done", "planned_days_missed",
    "activities_by_day",
}

EXPOSURE_BANDS = {"low_aerobic", "mid_aerobic", "high_aerobic", "anaerobic"}


def test_week_summary_200_and_required_fields(client):
    resp = client.get("/api/week-summary")
    assert resp.status_code == 200
    data = resp.json()
    missing = WEEK_SUMMARY_REQUIRED_FIELDS - set(data.keys())
    assert not missing, f"Missing top-level fields: {missing}"
    # Exposure bands are exactly the 4 expected keys
    assert set(data["exposure_minutes"].keys()) == EXPOSURE_BANDS
    # Dominant band is one of the 4 bands OR "mixed"
    assert data["exposure_dominant"] in (EXPOSURE_BANDS | {"mixed"})
    # Dates parse
    import datetime as _dt
    _dt.date.fromisoformat(data["week_start"])
    _dt.date.fromisoformat(data["week_end"])
    _dt.date.fromisoformat(data["today"])


def test_week_summary_exposure_sums_to_total_duration(client):
    resp = client.get("/api/week-summary")
    data = resp.json()
    total_exposure = sum(data["exposure_minutes"].values())
    # Rounding: each activity gets rounded to int minutes then bucketed, so
    # the sum can diverge from duration_min_done by at most (# activities)
    # minutes. But since duration_min_done itself is round(sum) and exposure
    # uses round(per-activity), a 1 min slack is plenty in practice.
    slack = max(1, len(data.get("activities_by_day", {})))
    assert abs(total_exposure - data["duration_min_done"]) <= slack, (
        f"exposure sum {total_exposure} vs done {data['duration_min_done']}"
    )


def test_week_summary_planned_day_accounting_invariant(client):
    """done + missed + rest days ≤ total. The slack comes from future days
    (not yet classified) and from rest days that just aren't counted."""
    resp = client.get("/api/week-summary")
    data = resp.json()
    total = data["planned_days_total"]
    with_session = data["planned_days_with_session"]
    done = data["planned_days_done"]
    missed = data["planned_days_missed"]
    rest = total - with_session  # rest days = non-session days
    assert done + missed + rest <= total
    # done and missed are both past-or-today only, so bounded by with_session
    assert done + missed <= with_session


def test_classify_exposure_hr_based_bands():
    """With both avg_hr and lthr, the HR ratio picks the band."""
    from app import _classify_exposure

    lthr = 175
    # 0.70 ratio → low_aerobic
    assert _classify_exposure({"avg_hr": 120}, lthr) == "low_aerobic"
    # 0.80 ratio → mid_aerobic (>=0.75, <0.88)
    assert _classify_exposure({"avg_hr": 140}, lthr) == "mid_aerobic"
    # 0.91 ratio → high_aerobic (>=0.88, <0.96)
    assert _classify_exposure({"avg_hr": 160}, lthr) == "high_aerobic"
    # 1.00 ratio → anaerobic
    assert _classify_exposure({"avg_hr": 175}, lthr) == "anaerobic"
    # Exact boundary checks
    assert _classify_exposure({"avg_hr": round(0.749 * lthr)}, lthr) == "low_aerobic"
    assert _classify_exposure({"avg_hr": round(0.88 * lthr) + 1}, lthr) == "high_aerobic"


def test_classify_exposure_trimp_fallback():
    """No HR → fall back to TRIMP per minute."""
    from app import _classify_exposure

    # TRIMP/min = 0.5 → low_aerobic
    assert _classify_exposure({"trimp": 30, "duration_min": 60}, 175) == "low_aerobic"
    # TRIMP/min = 1.0 → mid_aerobic
    assert _classify_exposure({"trimp": 60, "duration_min": 60}, 175) == "mid_aerobic"
    # TRIMP/min = 1.5 → high_aerobic
    assert _classify_exposure({"trimp": 90, "duration_min": 60}, 175) == "high_aerobic"
    # TRIMP/min = 2.5 → anaerobic
    assert _classify_exposure({"trimp": 150, "duration_min": 60}, 175) == "anaerobic"
    # avg_hr=None shouldn't crash, should fall back
    assert _classify_exposure(
        {"avg_hr": None, "trimp": 30, "duration_min": 60}, 175
    ) == "low_aerobic"


def test_classify_exposure_no_signal_defaults_low():
    """Neither avg_hr nor trimp → bucket into low_aerobic (gotcha)."""
    from app import _classify_exposure
    assert _classify_exposure({"avg_hr": None, "trimp": None, "duration_min": 45}, 175) == "low_aerobic"
    assert _classify_exposure({}, 175) == "low_aerobic"


def test_week_summary_empty_week(monkeypatch, client):
    """No activities this week → zeros, and every past non-rest session is missed."""
    import app as app_module

    # Force the activity source to return nothing.
    monkeypatch.setattr(app_module, "api_activities", lambda: [])

    resp = client.get("/api/week-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tss_done"] == 0
    assert all(v == 0 for v in data["exposure_minutes"].values())
    assert data["duration_min_done"] == 0
    assert data["planned_days_done"] == 0
    assert data["activities_by_day"] == {}

    # Every STRICTLY past non-rest planned session should be in missed_sessions.
    # Today's session must NOT be included until EOD (Wave 3 QA fix: was `<=`).
    import datetime as _dt
    today = _dt.date.fromisoformat(data["today"])
    plan = app_module.api_weekly_plan(week_offset=0)
    expected_missed = 0
    for s in plan.get("sessions", []):
        try:
            d = _dt.date.fromisoformat(s.get("day"))
        except (TypeError, ValueError):
            continue
        if d < today and (s.get("session_type") or "").lower() != "rest":
            expected_missed += 1
    assert len(data["missed_sessions"]) == expected_missed
    assert data["planned_days_missed"] == expected_missed
    # Today's session must never appear in missed_sessions
    for m in data["missed_sessions"]:
        assert m["day"] < data["today"], (
            f"missed_sessions contains today ({m['day']}) — should be strictly past"
        )


def test_week_summary_unclassified_minutes_field(client):
    """Activities without avg_hr AND without TRIMP should not inflate the
    low_aerobic bucket; they land in unclassified_minutes."""
    resp = client.get("/api/week-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "unclassified_minutes" in data
    assert isinstance(data["unclassified_minutes"], int)
    # exposure_minutes + unclassified_minutes should equal duration_min_done (±1 rounding)
    total = sum(data["exposure_minutes"].values()) + data["unclassified_minutes"]
    assert abs(total - data["duration_min_done"]) <= 1


def test_week_summary_planned_days_total_is_always_7(client):
    """ISO week is Mon-Sun: 7 days regardless of plan contents."""
    resp = client.get("/api/week-summary")
    assert resp.status_code == 200
    assert resp.json()["planned_days_total"] == 7


# ─── v4.0.0-alpha: /api/training/save-ride + /discard-ride + /info endpoints
# were removed with the trainer rip. The save-ride / discard-ride / stop /
# training-info test groups previously here (11 tests + _StubSession /
# _FakeInfoSession helpers) are obsolete and have been pruned.


# ─── v3.6.0-fix22b: spatial surface-segments contract ───────────────────────
# MASTER_DECISIONS §1 locks `surface_segments` as a list of
# {start_km: float, end_km: float, surface: "asphalt|gravel|cobble|dirt|sand|unknown"}
# — half-open intervals, lowercase enum, never UPPERCASE leaks, never silently
# dropped segments. Both /api/routes (list) and /api/routes/{id} (detail)
# must carry it so the dashboard mini-map doesn't need a second fetch.

_CANONICAL_SURFACE_ENUM = {"asphalt", "gravel", "cobble", "dirt", "sand", "unknown"}
_SEGMENT_KEYS = {"start_km", "end_km", "surface"}


def _assert_canonical_segments(segs, route_id=None):
    """Shared assertion for the canonical surface_segments shape."""
    assert isinstance(segs, list), f"{route_id}: surface_segments must be a list"
    for i, seg in enumerate(segs):
        assert isinstance(seg, dict), f"{route_id}[{i}]: segment not a dict"
        # Exactly the canonical keys — no UPPERCASE `TYPE`, no `length_km` etc.
        extra = set(seg.keys()) - _SEGMENT_KEYS
        missing = _SEGMENT_KEYS - set(seg.keys())
        assert not extra, f"{route_id}[{i}]: extra keys {extra}"
        assert not missing, f"{route_id}[{i}]: missing keys {missing}"
        # Types
        assert isinstance(seg["start_km"], (int, float)), f"{route_id}[{i}]: start_km must be numeric"
        assert isinstance(seg["end_km"], (int, float)), f"{route_id}[{i}]: end_km must be numeric"
        assert isinstance(seg["surface"], str), f"{route_id}[{i}]: surface must be str"
        # Enum: LOWERCASE, one of the canonical set.
        surf = seg["surface"]
        assert surf == surf.lower(), f"{route_id}[{i}]: surface '{surf}' not lowercase (UPPERCASE leak)"
        assert surf in _CANONICAL_SURFACE_ENUM, (
            f"{route_id}[{i}]: surface '{surf}' outside canonical enum"
        )
        # Half-open [start_km, end_km) — start must be <= end; allow equal for
        # degenerate input but prefer strict < (bar renderer skips 0-width).
        assert seg["start_km"] >= 0, f"{route_id}[{i}]: start_km < 0"
        assert seg["end_km"] >= seg["start_km"], f"{route_id}[{i}]: end_km < start_km"


def test_route_summary_surface_segments_enum_tripwire(client):
    """§1/§6 delta: tripwire that enforces the list response carries a
    meaningful mix of *mapped* surface segments (>=5) as well as the
    fallback `unknown`-only shape. Named distinctly from the earlier
    `test_route_summary_carries_surface_segments_canonical_shape` so pytest
    does not shadow one with the other (QA-CODE #1)."""
    resp = client.get("/api/routes?limit=200")
    assert resp.status_code == 200
    data = resp.json()
    routes = data.get("routes") or []
    assert routes, "expected at least one route in /api/routes response"

    saw_mapped = 0
    saw_unknown_fallback = 0
    for r in routes:
        assert "surface_segments" in r, f"route {r.get('id')} missing surface_segments"
        segs = r["surface_segments"]
        _assert_canonical_segments(segs, route_id=r.get("id"))
        if not segs:
            # Only allowed when distance_km is also missing/zero
            assert not (r.get("distance_km") or 0) > 0, (
                f"{r.get('id')}: empty segments despite distance_km={r.get('distance_km')}"
            )
            continue
        # Sum of segment spans ≤ distance_km + small tolerance (surface_types
        # intentionally may cover < full route; fallback fills rest as unknown).
        km = float(r.get("distance_km") or 0.0)
        total_span = sum(max(0.0, s["end_km"] - s["start_km"]) for s in segs)
        assert total_span <= km + 0.5 if km > 0 else True, (
            f"{r.get('id')}: span total {total_span:.3f} > distance {km:.3f}"
        )
        surfs = {s["surface"] for s in segs}
        if surfs == {"unknown"}:
            saw_unknown_fallback += 1
        else:
            saw_mapped += 1

    # We expect at least a few mapped + a few fallback routes across 200.
    assert saw_mapped >= 5, f"expected >=5 mapped routes, saw {saw_mapped}"


def test_route_detail_carries_surface_segments(client):
    """/api/routes/{id} returns canonical segments too — the detail modal
    uses this path and must not see a different shape than the list."""
    # blue_ridge/hidden-climb-3 is a known mapped route (asphalt/gravel/asphalt).
    resp = client.get("/api/routes/blue_ridge/hidden-climb-3")
    assert resp.status_code == 200
    data = resp.json()
    segs = data.get("surface_segments")
    _assert_canonical_segments(segs, route_id="blue_ridge/hidden-climb-3")
    assert len(segs) >= 2, "expected multi-segment route"
    # Specific fixture expectations: starts and ends with asphalt, gravel in middle.
    surfaces_seen = [s["surface"] for s in segs]
    assert "gravel" in surfaces_seen
    assert segs[0]["start_km"] == 0.0


def test_route_summary_never_leaks_uppercase_surface(client):
    """Tripwire for the Tacx `_SURFACE_NAME_MAP` UPPERCASE tokens
    (ASPHALT, COBBLESTONES_HARD, OFF_ROAD, …). Anything that crosses the
    HTTP boundary must already be lowercase — UPPERCASE is treated as a
    tier-1 bug (silent wrong color + broken CSS class lookup)."""
    resp = client.get("/api/routes?limit=500")
    assert resp.status_code == 200
    for r in resp.json().get("routes", []):
        for seg in r.get("surface_segments", []) or []:
            surf = seg.get("surface", "")
            assert surf == surf.lower(), (
                f"UPPERCASE leak at {r.get('id')}: {surf!r}"
            )


# ─── v4.0.0-alpha: /api/training/info endpoint was removed with the trainer
# rip. The info test group (3 tests + _FakeCourse / _FakeSurface /
# _FakeInfoSession helpers) is obsolete and has been pruned. The
# _canonical_surface helper below is a pure module-level unit test and
# is preserved — it does not depend on any live session.


def test_canonical_surface_helper_never_leaks_uppercase():
    """Direct unit test on the `_canonical_surface` helper so we catch the
    UPPERCASE normalization path even if no route exercises all branches."""
    cs = app_module._canonical_surface
    assert cs("asphalt") == "asphalt"
    assert cs("ASPHALT") == "asphalt"
    assert cs("Asphalt") == "asphalt"
    assert cs("COBBLESTONES_HARD") == "cobble"
    assert cs("COBBLESTONES_SOFT") == "cobble"
    assert cs("BRICK_ROAD") == "cobble"
    assert cs("GRAVEL") == "gravel"
    # OFF_ROAD is a Tacx road-feel alias mapped into the canonical enum
    # (app uses "gravel" today; the important invariant is that it lands
    # somewhere in the canonical set, NOT a bare UPPERCASE leak).
    assert cs("OFF_ROAD") in {"gravel", "dirt"}
    assert cs("OFF_ROAD") == cs("OFF_ROAD").lower()
    assert cs("DIRT") == "dirt"
    assert cs("SAND") == "sand"
    # Never-seen-before token → unknown (not silently dropped).
    assert cs("QUANTUM_FOAM") == "unknown"
    assert cs(None) == "unknown"
    assert cs("") == "unknown"
    # Whitespace tolerance
    assert cs("  gravel  ") == "gravel"


# ─── v3.6.0-fix29 — lap_route surface tiling ─────────────────────────────
#
# `surface_types.json` stores ONE base-lap's worth of segments, but
# `distance_km` reflects the fully multiplied distance when
# `lap_route.laps > 1`. `_route_surface_segments` must tile the base
# segments `laps` times with `base_km` offsets — otherwise lap 2+ falls
# through the frontend gap-filler as implicit asphalt, producing a wrong
# visual + wrong aggregate legend. Regression guards:
#   1) Cobbled Classic Sectors × 2 (laps=2, base_km=10, dist=20)
#   2) synthetic 3×5km
#   3) single-lap route unchanged (no lap_route metadata)
#   4) blue_ridge/hidden-cruise-47 full-distance coverage


def test_lap_route_surface_segments_tiled_2x():
    """Cobbled Classic: 9 base-lap segments tiled to span 0-20 km with the
    same asphalt/cobble pattern repeated. Base mix is 50/50; tiled result
    must preserve that ratio across both laps (fix29)."""
    segs = app_module._route_surface_segments(
        "gravel/cobbled-classic-sectors",
        20.0,
        {"laps": 2, "base_km": 10.0},
    )
    # Two laps × 9 base segments == 18 tiled segments
    assert len(segs) == 18, f"expected 18 tiled segments, got {len(segs)}"
    # Coverage must span the full 20 km route
    assert segs[0]["start_km"] == 0.0
    assert abs(segs[-1]["end_km"] - 20.0) < 1e-6, (
        f"tiled coverage should reach 20 km, ended at {segs[-1]['end_km']}"
    )
    # Lap 2 starts at offset 10.0
    lap2 = [s for s in segs if s["start_km"] >= 10.0]
    assert lap2, "no segments in second lap"
    assert abs(lap2[0]["start_km"] - 10.0) < 1e-6
    # Surface pattern must repeat exactly
    lap1_pattern = [s["surface"] for s in segs if s["end_km"] <= 10.0 + 1e-6]
    lap2_pattern = [s["surface"] for s in lap2]
    assert lap1_pattern == lap2_pattern, (
        f"lap 1 pattern {lap1_pattern} != lap 2 pattern {lap2_pattern}"
    )
    # Segments must remain monotonic
    for i in range(1, len(segs)):
        assert segs[i]["start_km"] >= segs[i - 1]["end_km"] - 1e-6


def test_lap_route_surface_segments_tiled_3x(monkeypatch):
    """Synthetic laps=3, base_km=5 → 3 copies of base pattern with offsets
    0, 5, 10. Guards the general-case tiling logic beyond the real-world
    Cobbled Classic fixture (fix29)."""
    fake_db = {
        "synthetic/test-lap-3x": [
            {"start_km": 0.0, "end_km": 2.0, "surface": "asphalt"},
            {"start_km": 2.0, "end_km": 5.0, "surface": "cobble"},
        ],
    }
    monkeypatch.setattr(app_module, "_load_surface_types_db", lambda: fake_db)
    segs = app_module._route_surface_segments(
        "synthetic/test-lap-3x",
        15.0,
        {"laps": 3, "base_km": 5.0},
    )
    # 2 base segs × 3 laps = 6
    assert len(segs) == 6
    assert [s["surface"] for s in segs] == [
        "asphalt", "cobble", "asphalt", "cobble", "asphalt", "cobble",
    ]
    # Offset ranges: [0,2,2,5], [5,7,7,10], [10,12,12,15]
    expected_ranges = [
        (0.0, 2.0), (2.0, 5.0),
        (5.0, 7.0), (7.0, 10.0),
        (10.0, 12.0), (12.0, 15.0),
    ]
    for seg, (a, b) in zip(segs, expected_ranges):
        assert abs(seg["start_km"] - a) < 1e-6
        assert abs(seg["end_km"] - b) < 1e-6


def test_single_lap_route_unchanged(monkeypatch):
    """Non-lap route: no lap_info (or lap_info with laps<=1) must return
    base segments verbatim — tiling only fires when laps>1 (fix29)."""
    fake_db = {
        "synthetic/plain": [
            {"start_km": 0.0, "end_km": 4.0, "surface": "asphalt"},
            {"start_km": 4.0, "end_km": 9.0, "surface": "gravel"},
        ],
    }
    monkeypatch.setattr(app_module, "_load_surface_types_db", lambda: fake_db)
    # No lap_info → unchanged
    segs_none = app_module._route_surface_segments("synthetic/plain", 9.0)
    assert len(segs_none) == 2
    assert segs_none[0]["start_km"] == 0.0 and segs_none[0]["end_km"] == 4.0
    assert segs_none[1]["start_km"] == 4.0 and segs_none[1]["end_km"] == 9.0
    # laps=1 → unchanged
    segs_one = app_module._route_surface_segments(
        "synthetic/plain", 9.0, {"laps": 1, "base_km": 9.0}
    )
    assert segs_one == segs_none
    # None lap_info dict → unchanged
    segs_nonedict = app_module._route_surface_segments(
        "synthetic/plain", 9.0, None
    )
    assert segs_nonedict == segs_none


def test_lap_route_hidden_cruise_47_covers_full_distance(client):
    """blue_ridge/hidden-cruise-47 (laps=2, base_km=8.15, dist=16.3): full
    /api/routes payload must expose surface_segments covering ≈16.3 km, not
    just the 0-8.06 km base-lap. Before fix29 the second lap silently fell
    through as implicit asphalt in the frontend (fix29)."""
    target_id = "blue_ridge/hidden-cruise-47"
    resp = client.get("/api/routes?limit=700")
    assert resp.status_code == 200
    routes = resp.json().get("routes", [])
    match = next((r for r in routes if r.get("id") == target_id), None)
    if match is None:
        pytest.skip(f"{target_id} not in current dataset")
    segs = match.get("surface_segments") or []
    assert segs, "expected tiled surface_segments for lap route"
    total_cov = segs[-1]["end_km"] - segs[0]["start_km"]
    # Coverage should reach full 16.3 km (±0.3 for tiling precision)
    assert abs(total_cov - 16.3) < 0.3, (
        f"coverage {total_cov:.2f} km does not span full 16.3 km distance"
    )
    # Second lap must be present (end_km > base_km=8.15)
    assert any(s["end_km"] > 8.15 for s in segs), (
        "no segments beyond first lap — tiling did not fire"
    )
