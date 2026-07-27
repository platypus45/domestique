"""v3.6.0 — the rating controls reading back what is stored, and the rating's
own shape.

This file also covered the production path into block grading. That feature is
not surfaced in v3.6.0 (see the note at the `execution` payload in app.py and
docs/SCIENCE.md): inferring which prescribed blocks a rider did from an
unlabelled lap list produced a different class of confident wrong verdict in
each of four attempts. The grader and its library-scale harness stay in
tests/test_357_block_evaluation.py; the tests for the removed wiring went with
the wiring.

What remains here matters on its own: a value in the database that the form
re-renders blank is indistinguishable, to the rider, from a value that was
never saved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app as app_module


# ── the rating reads back ────────────────────────────────────────────────────

def _dash() -> str:
    return (Path(app_module.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")


def test_the_morning_form_preselects_what_is_stored():
    src = _dash()
    assert "function showMorningForm(stored)" in src
    assert "stored.readiness_to_train" in src
    assert "window._todayLog" in src


def test_the_ride_rating_reflects_its_own_save():
    """setRideRpe touched no local state, and the modal caches its payload — so
    the control re-rendered unselected on the next open."""
    src = _dash()
    assert "rpe-row-" in src
    assert "cached.rpe = rpe" in src


# ── the rating's own shape ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (None, None), ("", None), (7, 7), ("7", 7), (7.0, 7),
])
def test_a_rating_is_parsed_as_a_whole_number(raw, expect):
    assert app_module._parse_rtt(raw) == expect


@pytest.mark.parametrize("raw", [5.5, "5.5", "seven", True, [7], {"a": 1}])
def test_a_rating_that_is_not_a_whole_number_is_rejected(raw):
    """`int(raw)` truncated instead of rejecting — 5.5 was stored as 5. An
    upgraded install has no CHECK on this column, so this is the only guard."""
    with pytest.raises(ValueError):
        app_module._parse_rtt(raw)
