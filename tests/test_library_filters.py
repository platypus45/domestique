"""v4.2.0 IMPL-LIBRARY: /api/workouts query-param regression suite.

Exercises every filter / sort / search param the library browser surfaces,
plus two combinations. Uses the real WORKOUT_DIR (3000+ ZWO files) via
``TestClient`` so we exercise the same parse path the dashboard hits.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402

client = TestClient(app_module.app)


def _get_workouts(**params) -> list[dict]:
    """Wrapper: GET /api/workouts with query params, asserting 200."""
    r = client.get("/api/workouts", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    return body


# ── Smoke / baseline ─────────────────────────────────────────────────────────


def test_default_returns_workouts():
    """No filters → library populated."""
    out = _get_workouts(limit=20)
    assert len(out) > 0
    sample = out[0]
    # Field contract: each row has a score, name, file, duration.
    for k in ("Score", "Name", "File", "Duration(min)"):
        assert k in sample, f"missing {k!r} in row"


# ── Individual params ────────────────────────────────────────────────────────


def test_min_score_filter():
    """min_score=7 returns only rows with Score>=7."""
    out = _get_workouts(min_score=7, limit=200)
    for w in out:
        assert int(w["Score"]) >= 7


def test_min_score_zero_includes_low_scores():
    """min_score=0 includes the LOW band rows.

    v1.8.25 — limit raised 3000 → 6000. The library grew past 4100 files with
    >3500 scoring ≥3; the old 3000 limit (score-desc) cut the low-score tail off
    page one, so min(scores) never reached the LOW band. Limit must exceed the
    library size to prove min_score=0 surfaces low-band rows.
    """
    out = _get_workouts(min_score=0, limit=6000)
    scores = {int(w["Score"]) for w in out}
    # The library has at least one low-band workout.
    assert min(scores) <= 3


def test_duration_min_max_window():
    """duration_min=30 & duration_max=45 → all rows in [30, 45] minutes."""
    out = _get_workouts(duration_min=30, duration_max=45, limit=500)
    assert out, "expected some workouts in 30-45 min window"
    for w in out:
        d = float(w["Duration(min)"])
        assert 30 <= d <= 45, f"{w['File']} dur={d} outside window"


def test_duration_min_only():
    """duration_min alone respects the lower bound."""
    out = _get_workouts(duration_min=90, limit=500)
    for w in out:
        assert float(w["Duration(min)"]) >= 90


def test_duration_max_only():
    """duration_max alone respects the upper bound."""
    out = _get_workouts(duration_max=20, limit=500)
    for w in out:
        assert float(w["Duration(min)"]) <= 20


def test_session_type_filter():
    """session_type=Endurance returns only Endurance-protocol rows."""
    out = _get_workouts(session_type="Endurance", limit=200)
    assert out
    for w in out:
        assert "endurance" in (w.get("Protocol") or "").lower()


def test_tags_filter_ftp_test():
    """tags=ftp_test returns only ftp_test-tagged rows (small set)."""
    out = _get_workouts(tags="ftp_test", limit=200)
    assert out, "expected at least 1 ftp_test workout in the library"
    for w in out:
        tagset = {t.lower() for t in (w.get("Tags") or [])}
        assert "ftp_test" in tagset, f"{w['File']} has no ftp_test tag"


def test_search_substring_match_name():
    """search=ramp narrows to rows whose Name or File contains 'ramp'."""
    out = _get_workouts(search="ramp", limit=500)
    assert out, "library should have at least one 'ramp' workout"
    for w in out:
        hay = (w.get("Name", "") + " " + w.get("File", "")).lower()
        assert "ramp" in hay


def test_search_case_insensitive():
    """Search is case-insensitive."""
    lo = _get_workouts(search="vo2max", limit=200)
    hi = _get_workouts(search="VO2MAX", limit=200)
    assert {w["File"] for w in lo} == {w["File"] for w in hi}


def test_content_class_exact_match():
    """content_class=vo2max returns only rows whose content_class is vo2max.

    Skipped gracefully if the on-disk content cache is missing (the
    classifier script hasn't been run).
    """
    out = _get_workouts(content_class="vo2max", limit=500)
    if not out:
        pytest.skip("content cache empty — run scripts/classify_library_content.py")
    for w in out:
        assert w.get("content_class") == "vo2max"


def test_sort_score_desc_default():
    """sort=score_desc returns scores in non-increasing order."""
    out = _get_workouts(sort="score_desc", limit=50)
    scores = [int(w["Score"]) for w in out]
    assert scores == sorted(scores, reverse=True)


def test_sort_score_asc():
    """sort=score_asc returns scores in non-decreasing order."""
    out = _get_workouts(sort="score_asc", limit=50)
    scores = [int(w["Score"]) for w in out]
    assert scores == sorted(scores)


def test_sort_duration_asc():
    """sort=duration_asc returns durations in non-decreasing order."""
    out = _get_workouts(sort="duration_asc", limit=50)
    durs = [float(w["Duration(min)"]) for w in out]
    assert durs == sorted(durs)


def test_sort_duration_desc():
    """sort=duration_desc returns durations in non-increasing order."""
    out = _get_workouts(sort="duration_desc", limit=50)
    durs = [float(w["Duration(min)"]) for w in out]
    assert durs == sorted(durs, reverse=True)


def test_sort_name_asc():
    """sort=name_asc returns names alphabetised, case-insensitive."""
    out = _get_workouts(sort="name_asc", limit=30)
    names = [w["Name"].lower() for w in out]
    assert names == sorted(names)


def test_sort_name_desc():
    """sort=name_desc returns names reverse-alphabetised."""
    out = _get_workouts(sort="name_desc", limit=30)
    names = [w["Name"].lower() for w in out]
    assert names == sorted(names, reverse=True)


# ── Combinations (spec requires ≥2) ──────────────────────────────────────────


def test_combo_content_class_plus_min_score():
    """content_class=vo2max + min_score=7 narrows correctly."""
    out = _get_workouts(content_class="vo2max", min_score=7, limit=200)
    if not out:
        pytest.skip("content cache or library too narrow for vo2max+score≥7")
    for w in out:
        assert w.get("content_class") == "vo2max"
        assert int(w["Score"]) >= 7


def test_combo_duration_window_plus_search():
    """duration window 30-60 + search='vo2' returns intersection."""
    out = _get_workouts(duration_min=30, duration_max=60, search="vo2", limit=200)
    if not out:
        pytest.skip("library has no vo2 workouts in 30-60 min window")
    for w in out:
        d = float(w["Duration(min)"])
        assert 30 <= d <= 60
        hay = (w.get("Name", "") + " " + w.get("File", "")).lower()
        assert "vo2" in hay


# ── /api/workouts/tags endpoint ──────────────────────────────────────────────


def test_tags_endpoint_returns_sorted_list():
    """GET /api/workouts/tags returns {"tags": [...]} sorted unique."""
    r = client.get("/api/workouts/tags")
    assert r.status_code == 200
    j = r.json()
    assert "tags" in j
    tags = j["tags"]
    assert isinstance(tags, list)
    assert tags == sorted(set(tags)), "tags must be sorted + unique"


def test_tags_endpoint_includes_ftp_test():
    """ftp_test tag is present (the library has ≥6 ftp_test workouts)."""
    r = client.get("/api/workouts/tags")
    j = r.json()
    assert "ftp_test" in j.get("tags", [])
