"""3.14.0 W1 + W2 — the intervals.icu weight (owner decision O2).

W1: the scale weight logged in intervals.icu fills the weight history (the
Body card), never over a weight the rider logged that day; display only.
W2: the profile weight follows intervals.icu ONLY after the rider ticks "Use
my intervals.icu weight". A typed weight is "manual"; a profile with no source
(every profile before 3.14.0) is the rider's and never moves on its own.

Grill fixes pinned here: H3 (the tick goes through one explicit hand-back
writer, because the fact table refuses intervals.icu over a typed weight), M4
(a typed weight always stamps "manual"), M5 (one definition of "the
intervals.icu weight": the newest scale reading of 30 days, else the athlete
setting for the wizard's prefill), M6 (a purge keeps a followed weight as the
rider's; another athlete's account asks to check it), L4 (0.5 kg dead band),
L6 (the wizard's wellness window rolls).
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module                      # noqa: E402
import db                                     # noqa: E402
from test_icu_push import stub                # noqa: E402,F401  (fixture)

DAY = date(2026, 11, 10)
DASH = ROOT / "src" / "templates" / "dashboard.html"


def _iso(days_ago=0):
    return (DAY - timedelta(days=days_ago)).isoformat()


def _sync(rows):
    with mock.patch.object(db, "fetch_wellness", return_value=rows):
        return db.sync_wellness(days=60)


def _rec(days_ago=0, **fields):
    return {"id": _iso(days_ago), "ctl": 40.0, "atl": 38.0, **fields}


def _athlete(stub):
    return json.loads((stub.pm.active_dir / "athlete.json").read_text(encoding="utf-8"))


def _own(stub, weight, source):
    stub.pm._athlete["weight_kg"] = weight
    if source is None:
        stub.pm._athlete.pop("weight_source", None)
    else:
        stub.pm._athlete["weight_source"] = source
    stub.pm._write_json(stub.pm.active_dir / "athlete.json", stub.pm._athlete)


@pytest.fixture()
def rider(stub, freeze_clock):
    freeze_clock(DAY)
    return stub


# ── W1 ───────────────────────────────────────────────────────────────────────

def test_t17_the_scale_weight_fills_the_history_and_nothing_else(rider):
    db.log_metric(_iso(1), "weight", 70.0, "settings")
    _sync([_rec(0, weight=72.4), _rec(1, weight=71.0), _rec(2, weight=0),
           _rec(3, weight=250), _rec(4, weight="72")])
    rows = {r["date"]: r for r in db.query_metric_history("weight", 365)}
    assert rows[_iso(0)]["value"] == 72.4 and rows[_iso(0)]["source"] == "intervals.icu"
    assert (rows[_iso(1)]["value"], rows[_iso(1)]["source"]) == (70.0, "settings")
    for d in (_iso(2), _iso(3), _iso(4)):
        assert d not in rows
    assert rider.pm._athlete["weight_kg"] == 70.0, "history only: the profile did not move"


# ── W2 ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", [None, "manual"], ids=["no-source", "typed"])
def test_t18_the_riders_weight_never_follows_on_its_own(rider, source):
    _own(rider, 68.0, source)
    _sync([_rec(0, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 68.0
    assert _athlete(rider)["weight_kg"] == 68.0


def test_t18_a_follower_gets_the_scale_weight_to_the_tenth(rider):
    _own(rider, 68.0, "icu")
    _sync([_rec(0, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 72.4, "the 0.1 kg was cut (int cast)"
    assert _athlete(rider)["weight_kg"] == 72.4
    assert rider.pm._athlete["weight_source"] == "icu"


def test_h3_tick_untick_and_tick_again(rider):
    c = rider.client
    assert c.post("/api/settings", json={"weight": 68.0}).status_code == 200
    assert rider.pm._athlete["weight_source"] == "manual"
    _sync([_rec(2, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 68.0

    r = c.post("/api/settings", json={"weight": 68.0, "weight_follow_icu": True})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert rider.pm._athlete["weight_source"] == "icu"
    assert rider.pm._athlete["weight_kg"] == 72.4, "the newest reading lands at once"
    assert s["weight_follow_icu"] is True and s["weight"] == 72.4
    assert s["weight_icu_latest"] == {"date": _iso(2), "value": 72.4}

    r = c.post("/api/settings", json={"weight_follow_icu": False})
    assert r.status_code == 200
    assert rider.pm._athlete["weight_source"] == "manual"
    assert rider.pm._athlete["weight_kg"] == 72.4, "unticking keeps the number"

    _sync([_rec(1, weight=71.0)])
    assert rider.pm._athlete["weight_kg"] == 72.4
    c.post("/api/settings", json={"weight_follow_icu": True})
    assert rider.pm._athlete["weight_source"] == "icu"
    assert rider.pm._athlete["weight_kg"] == 71.0, "a second tick must work too"


def test_h3_a_tick_without_a_recent_reading_moves_only_the_source(rider):
    _own(rider, 68.0, "manual")
    rider.client.post("/api/settings", json={"weight_follow_icu": True})
    assert (rider.pm._athlete["weight_kg"], rider.pm._athlete["weight_source"]) == (68.0, "icu")
    s = rider.client.get("/api/settings").json()
    assert s["weight_follow_icu"] is True and s["weight_icu_latest"] is None


def test_m4_a_typed_weight_ends_the_follow(rider):
    _own(rider, 72.0, "icu")
    r = rider.client.post("/api/settings", json={"weight": 68.0})   # no tick in the body
    assert r.status_code == 200
    assert rider.pm._athlete["weight_source"] == "manual"
    _sync([_rec(0, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 68.0, "the sync put intervals.icu back over a typed weight"


def test_m4_a_tick_ignores_a_weight_sent_with_it(rider):
    _own(rider, 68.0, "manual")
    _sync([_rec(1, weight=72.4)])
    rider.client.post("/api/settings", json={"weight": 50.0, "weight_follow_icu": True})
    assert rider.pm._athlete["weight_kg"] == 72.4
    assert not any(r["source"] == "settings" for r in db.query_metric_history("weight", 365))


# ── M5: one definition of "the intervals.icu weight" ────────────────────────

def test_m5_the_follow_ignores_a_reading_older_than_the_window(rider):
    _own(rider, 75.0, "icu")
    _sync([_rec(db.ICU_WEIGHT_RECENT_DAYS + 10, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 75.0, "an old reading replaced today's weight"
    _sync([_rec(3, weight=73.1)])
    assert rider.pm._athlete["weight_kg"] == 73.1


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def _wizard_weight(monkeypatch, wellness, urls=None):
    import httpx

    def fake_get(url, *a, **k):
        if urls is not None:
            urls.append(url)
        if "/wellness" in url:
            return _Resp(200, wellness)
        return _Resp(200, {"name": "Rider", "weight": 75.0})
    monkeypatch.setattr(httpx, "get", fake_get)
    from fastapi.testclient import TestClient
    r = TestClient(app_module.app).post("/api/setup/test-icu",
                                        json={"api_key": "k", "athlete_id": "i1"})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return r.json()["weight"]


def test_m5_the_wizard_prefills_the_same_weight_the_follow_uses(monkeypatch, freeze_clock):
    freeze_clock(DAY)
    recent = [_rec(40, weight=70.1), _rec(3, weight=72.4),
              {"id": _iso(1), "sportInfo": [{"eftp": 250}]}]
    assert _wizard_weight(monkeypatch, recent) == 72.4
    assert _wizard_weight(monkeypatch, [_rec(40, weight=70.1)]) == 75.0, (
        "no reading in the window: the athlete setting")


def test_l6_the_wizard_window_rolls(monkeypatch, freeze_clock):
    later = date(2027, 2, 1)
    freeze_clock(later)
    urls = []
    _wizard_weight(monkeypatch, [], urls)
    w = next(u for u in urls if "/wellness" in u)
    assert f"newest={later.isoformat()}" in w
    assert f"oldest={(later - timedelta(days=365)).isoformat()}" in w


# ── M6: the purge ────────────────────────────────────────────────────────────

def test_m6_a_disconnect_keeps_a_followed_weight_as_the_riders(rider):
    _own(rider, 72.4, "icu")
    db.purge_profile_data(rider.pid)
    a = _athlete(rider)
    assert (a["weight_kg"], a["weight_source"]) == (72.4, "manual")
    assert "weight_needs_check" not in a
    assert app_module._planner_athlete() is not None, "event targets lost their weight"


def test_m6_another_athlete_asks_the_rider_to_check_the_weight(rider):
    _own(rider, 72.4, "icu")
    db.purge_profile_data(rider.pid, athlete_changed=True)
    a = _athlete(rider)
    assert (a["weight_kg"], a["weight_source"], a["weight_needs_check"]) == (72.4, "manual", True)
    assert rider.client.get("/api/settings").json()["weight_needs_check"] is True
    rider.client.post("/api/settings", json={"weight": 70.0})
    assert "weight_needs_check" not in _athlete(rider)
    assert rider.client.get("/api/settings").json()["weight_needs_check"] is False


def test_m6_a_typed_weight_is_not_flagged_after_an_athlete_change(rider):
    _own(rider, 68.0, "manual")
    db.purge_profile_data(rider.pid, athlete_changed=True)
    a = _athlete(rider)
    assert (a["weight_kg"], a["weight_source"]) == (68.0, "manual")
    assert "weight_needs_check" not in a


def test_m6_the_oauth_callback_reports_the_athlete_change(rider):
    calls = []

    class _Tok:
        status_code = 200

        def json(self):
            return {"access_token": "ATOK", "athlete": {"id": "i-new", "name": "N"}}

    app_module._icu_oauth_states["SW"] = {"profile_id": rider.pid, "ts": 9e12}
    with mock.patch("httpx.post", return_value=_Tok()), \
         mock.patch.object(app_module, "_icu_profile_stored_athlete_id", return_value="i-old"), \
         mock.patch.object(app_module, "_save_icu_token_for_profile"), \
         mock.patch.object(db, "purge_profile_data",
                           side_effect=lambda pid, **kw: calls.append((pid, kw))):
        r = rider.client.get("/oauth/icu/callback?code=C&state=SW", follow_redirects=False)
    assert "icu=connected" in r.headers["location"]
    assert calls == [(rider.pid, {"athlete_changed": True})]


# ── L4 ───────────────────────────────────────────────────────────────────────

def test_l4_the_follow_ignores_scale_noise(rider):
    band = db.WEIGHT_FOLLOW_MIN_CHANGE_KG
    _own(rider, 72.0, "icu")
    _sync([_rec(1, weight=round(72.0 + band * 0.6, 1))])
    assert rider.pm._athlete["weight_kg"] == 72.0
    _sync([_rec(0, weight=round(72.0 + band * 1.2, 1))])
    assert rider.pm._athlete["weight_kg"] == round(72.0 + band * 1.2, 1)


# ── the Settings row and the Body card ───────────────────────────────────────

def test_the_settings_form_sends_the_tick_and_no_weight_with_it():
    src = DASH.read_text(encoding="utf-8")
    assert 'id="set-weight-icu"' in src and "Use my intervals.icu weight" in src
    start = src.index("async function saveSettings()")
    body = src[start:src.index("\n}\n", start)]
    assert "body.weight_follow_icu = !!_wIcu.checked" in body
    assert "delete body.weight" in body
    start = src.index("async function loadSettings()")
    assert "_renderWeightFollow(d)" in src[start:start + 1500]


def test_the_body_card_headline_is_the_profile_weight():
    src = DASH.read_text(encoding="utf-8")
    start = src.index("async function loadBodyPerf()")
    body = src[start:src.index("\n}\n", start)]
    assert "s.weight_source" in body and "/api/settings" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_settings_note_says_what_happens():
    from test_331_surfaces import _extract_js_function, _run_node
    src = DASH.read_text(encoding="utf-8")
    harness = ("const esc = s => String(s);\n" + _extract_js_function(src, "_weightFollowNote")
               + """
const a = _weightFollowNote({weight_icu_latest: {date: '2026-11-08', value: 72.4}}, true);
if (!a.includes('72.4 kg on 2026-11-08')) throw new Error(a);
const b = _weightFollowNote({weight_icu_latest: null}, true);
if (!b.includes('No intervals.icu weight in the last 30 days')) throw new Error(b);
if (_weightFollowNote({}, false) !== '') throw new Error('unticked must be quiet');
const c = _weightFollowNote({weight_needs_check: true}, false);
if (!c.includes('linked before')) throw new Error(c);
console.log('OK');
""")
    _run_node(harness)
