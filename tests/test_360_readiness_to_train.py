"""v3.6.0 — readiness-to-train, and the sign trap it walked into.

Why this exists at all: `daily_log` had **0 rows in 180 days** on the live
profile, so the readiness composite's `subjective` channel had never fired
once. Ten Haaf 2017 (PMID 27834554) found pre-session fatigue +
readiness-to-train discriminated functional overreaching in 30 cyclists at 3
days (78% correct, sens 79%, spec 77%) — and readiness-to-train is NOT one of
the four Hooper items. Adding it is what makes the subjective channel exist.

The trap, caught by an adversarial grill before it shipped:
`_get_soreness_subjective` combines the Hooper items with **min()**, not a
mean ("any limb weak"). The Hooper items run 1=best/7=worst and get inverted
by the mapping; readiness-to-train runs 1-10 where HIGH = READY. Appending it
to that min() would have (a) made one slider the sole determinant of the
channel, since min is a one-way ratchet, and (b) inverted the sign on any
mis-read of direction. It therefore enters as its own term.
"""
from __future__ import annotations

import pytest

import app as app_module
import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.close_all_connections()
    db.init_db()
    yield
    db.close_all_connections()


# ── schema + persistence ────────────────────────────────────────────────────

def test_column_exists_after_migration(fresh_db):
    cols = [r[1] for r in db.get_db().execute("PRAGMA table_info(daily_log)")]
    assert "readiness_to_train" in cols


def test_stored_and_returned(fresh_db):
    e = db.upsert_daily_log("2026-07-26", 3, 3, 2, 3, 4, readiness_to_train=8)
    assert e["readiness_to_train"] == 8


def test_omitting_it_does_not_null_a_stored_rating(fresh_db):
    """INSERT OR REPLACE rewrites the whole row, so a Hooper-only save would
    have wiped a rating the rider already gave."""
    db.upsert_daily_log("2026-07-26", 3, 3, 2, 3, 4, readiness_to_train=8)
    e = db.upsert_daily_log("2026-07-26", 4, 4, 4, 4, 4)      # no rtt
    assert e["readiness_to_train"] == 8


@pytest.mark.parametrize("bad", [0, 11, 99, -1])
def test_out_of_range_rejected(fresh_db, bad):
    with pytest.raises(ValueError):
        db.upsert_daily_log("2026-07-26", 4, 4, 4, 4, 4, readiness_to_train=bad)


def test_absent_stays_none(fresh_db):
    e = db.upsert_daily_log("2026-07-26", 4, 4, 4, 4, 4)
    assert e["readiness_to_train"] is None


# ── the sign trap ───────────────────────────────────────────────────────────

def _subj(monkeypatch, **log):
    base = {"soreness": 4, "fatigue": 4, "stress": 4, "sleep_quality": 4}
    base.update(log)
    monkeypatch.setattr(app_module.db, "get_daily_log_today", lambda: base)
    return app_module._get_soreness_subjective()


def test_high_readiness_raises_the_channel(monkeypatch):
    """HIGH readiness-to-train = ready = a HIGHER subjective score. If this
    ever inverts, the composite reads a fresh rider as a wrecked one."""
    low = _subj(monkeypatch, readiness_to_train=2)
    high = _subj(monkeypatch, readiness_to_train=9)
    assert high > low, f"direction inverted: rtt 9 -> {high}, rtt 2 -> {low}"


def test_high_hooper_values_still_lower_the_channel(monkeypatch):
    """The Hooper items keep their 1=best/7=worst direction."""
    good = _subj(monkeypatch, fatigue=1, readiness_to_train=8)
    bad = _subj(monkeypatch, fatigue=7, readiness_to_train=8)
    assert bad < good


def test_readiness_is_not_folded_into_the_min(monkeypatch):
    """The bug that was nearly shipped: inside a min(), one good slider could
    not raise the score and one bad one would decide it outright."""
    worst_hooper = _subj(monkeypatch, fatigue=7, readiness_to_train=None)
    with_good_rtt = _subj(monkeypatch, fatigue=7, readiness_to_train=10)
    assert with_good_rtt > worst_hooper, (
        "readiness-to-train must be able to move the channel UP; if it is "
        "inside the min() it can only ever drag it down")


def test_hooper_min_semantics_preserved_within_its_own_group(monkeypatch):
    """One bad Hooper item still dominates the other three (Hooper's intent)."""
    monkeypatch.setattr(app_module.db, "get_daily_log_today",
                        lambda: {"soreness": 1, "fatigue": 1, "stress": 1,
                                 "sleep_quality": 7})
    one_bad = app_module._get_soreness_subjective()
    monkeypatch.setattr(app_module.db, "get_daily_log_today",
                        lambda: {"soreness": 1, "fatigue": 1, "stress": 1,
                                 "sleep_quality": 1})
    all_good = app_module._get_soreness_subjective()
    assert one_bad < all_good


def test_readiness_alone_still_produces_a_channel(monkeypatch):
    """A rider who only answers the readiness slider must still get a
    subjective score — previously the channel needed Hooper items."""
    monkeypatch.setattr(app_module.db, "get_daily_log_today",
                        lambda: {"readiness_to_train": 7})
    assert app_module._get_soreness_subjective() == 7.0


def test_no_input_returns_none(monkeypatch):
    monkeypatch.setattr(app_module.db, "get_daily_log_today", lambda: {})
    assert app_module._get_soreness_subjective() is None
    monkeypatch.setattr(app_module.db, "get_daily_log_today", lambda: None)
    assert app_module._get_soreness_subjective() is None


# ── UI contract ─────────────────────────────────────────────────────────────

def test_form_offers_it_optionally_and_never_pre_fills():
    from pathlib import Path
    src = (Path(app_module.__file__).parent / "templates" / "dashboard.html"
           ).read_text(encoding="utf-8")
    assert "_renderReadinessRow" in src
    assert "readiness_to_train: null" in src, "must not invent a default rating"
    assert "Ready to train?" in src
    assert "(optional)" in src or "optional" in src
    # Sent only when actually set.
    assert "_hooperDraft.readiness_to_train != null" in src
