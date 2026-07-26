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


def test_one_optional_tap_cannot_cancel_the_worst_hooper_answer(monkeypatch):
    """Caught by an adversarial pass before shipping. With an equal-weight mean,
    the worst possible sleep answer plus three merely-poor ones — a combination
    that trips neither the soreness cap nor the Hooper-sum gate — was lifted
    from readiness 56 to 66 by a single tap of "ready = 10", cancelling the
    readiness-under-60 all-Z2 rule. One optional, unvalidated self-rating must
    not overturn four answered questions."""
    import readiness as R

    def _score(sub):
        return R.compute_readiness(
            ln_rmssd_7d=3.2, swc_lower=3.0, swc_upper=3.4, tsb=-4.0,
            sleep_h=6.5, rhr_delta=1.0, subjective=sub)["score"]

    monkeypatch.setattr(app_module.db, "get_daily_log_today",
                        lambda: {"soreness": 3, "fatigue": 3, "stress": 3,
                                 "sleep_quality": 7})
    worst = app_module._get_soreness_subjective()
    monkeypatch.setattr(app_module.db, "get_daily_log_today",
                        lambda: {"soreness": 3, "fatigue": 3, "stress": 3,
                                 "sleep_quality": 7, "readiness_to_train": 10})
    tapped = app_module._get_soreness_subjective()
    assert tapped > worst, "it must still be able to move the channel up"
    assert _score(worst) < 60 and _score(tapped) < 60, (
        "a single rating must not carry the score across the all-Z2 threshold")


def test_hooper_stays_the_dominant_term(monkeypatch):
    """Bounded influence, both directions."""
    for rtt, direction in ((1, -1), (10, 1)):
        monkeypatch.setattr(app_module.db, "get_daily_log_today",
                            lambda: {"soreness": 4, "fatigue": 4, "stress": 4,
                                     "sleep_quality": 4})
        base = app_module._get_soreness_subjective()
        monkeypatch.setattr(app_module.db, "get_daily_log_today",
                            lambda r=rtt: {"soreness": 4, "fatigue": 4,
                                           "stress": 4, "sleep_quality": 4,
                                           "readiness_to_train": r})
        moved = app_module._get_soreness_subjective()
        assert (moved - base) * direction > 0
        assert abs(moved - base) <= 2.5


def test_the_rating_is_asymmetric_because_the_evidence_is(monkeypatch):
    """Ten Haaf 2017 found readiness-to-train discriminating on the LOW side;
    the upward direction has no such support and one direct contradiction
    (Sansone 2023 — better wellness produced HIGHER effort because riders did
    more work). So a low rating pulls at full weight and a high one nudges."""
    def _s(rtt):
        monkeypatch.setattr(app_module.db, "get_daily_log_today",
                            lambda: {"soreness": 4, "fatigue": 4, "stress": 4,
                                     "sleep_quality": 4,
                                     **({"readiness_to_train": rtt}
                                        if rtt is not None else {})})
        return app_module._get_soreness_subjective()
    base = _s(None)
    assert abs(_s(1) - base) > abs(_s(10) - base), (
        "an unready rating must carry more weight than a ready one")


def test_the_documented_bound_holds_across_every_hooper_answer(monkeypatch):
    """The promise made in the code comment, asserted: at most 0.9 up and 2.25
    down on the 1-10 channel, over the whole Hooper grid."""
    up = down = 0.0
    for a in range(1, 8):
        for b in range(1, 8):
            log = {"sleep_quality": a, "fatigue": b, "stress": 4, "soreness": 4}
            monkeypatch.setattr(app_module.db, "get_daily_log_today",
                                lambda l=log: l)
            base = app_module._get_soreness_subjective()
            for r in range(1, 11):
                monkeypatch.setattr(
                    app_module.db, "get_daily_log_today",
                    lambda l=log, rr=r: {**l, "readiness_to_train": rr})
                d = app_module._get_soreness_subjective() - base
                up, down = max(up, d), min(down, d)
    assert up <= 0.9 + 1e-9, up
    assert down >= -2.25 - 1e-9, down


def test_a_mid_range_rating_cannot_lift_a_rest_day_into_riding(monkeypatch):
    """The case that failed the first fix: Hooper answers that trip neither the
    soreness cap nor the Hooper-sum gate, with the objective channels already
    poor. A rating of 7 turned forced rest into a Z2 ride."""
    import readiness as R

    def _score(rtt):
        monkeypatch.setattr(
            app_module.db, "get_daily_log_today",
            lambda: {"sleep_quality": 1, "fatigue": 1, "soreness": 1,
                     "stress": 4,
                     **({"readiness_to_train": rtt} if rtt else {})})
        sub = app_module._get_soreness_subjective()
        return R.compute_readiness(
            ln_rmssd_7d=2.95, swc_lower=3.0, swc_upper=3.4, tsb=-12.0,
            sleep_h=5.5, rhr_delta=3.0, subjective=sub)["score"]

    assert _score(None) < 40, "precondition: this day is a forced-rest day"
    assert _score(7) < 40, "a mid-range rating must not lift it out of rest"


def test_readiness_alone_still_produces_a_channel_but_pulled_to_neutral(monkeypatch):
    """A rider with only the readiness slider still gets a subjective score,
    but an un-anchored self-rating must not drive the channel to an extreme on
    its own — so it is pulled toward neutral. (Not reachable through
    /api/daily-log, which requires all four Hooper items; guarded so a future
    caller cannot hand one optional slider the whole channel.)"""
    def _s(rtt):
        monkeypatch.setattr(app_module.db, "get_daily_log_today",
                            lambda: {"readiness_to_train": rtt})
        return app_module._get_soreness_subjective()
    assert _s(7) is not None
    assert 5.5 < _s(7) < 7.0          # moved up from neutral, not all the way
    assert 1.0 < _s(1) < 5.5          # and down, likewise
    assert _s(10) > _s(1)


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
