"""v1.3.3 perf regression — dashboard frontpage must paint cards quickly.

Pre-fix the home tab cards (LEG CHECK, READINESS, THIS WEEK, eFTP, BODY &
PERFORMANCE, TODAY'S RECOMMENDATION, RECENT ACTIVITIES, POWER DURATION
CURVE, etc.) were stuck on "Loading…" for 5-10s after a v1.3.2 ship. Two
contributing roots, fixed together:

1. ``loadHome()`` ``await loadTodaySession()`` blocked every card painted
   below it (recent activities, power curve, body perf, readiness
   composite, morning log) until ``/api/today-session`` resolved. With
   that endpoint at ~200-700ms warm and worse cold, the user saw the
   page spin instead of paint.

2. ``/api/calendar`` (also fired on home-load) spent ~250ms in TWO
   uncached ICU HTTP round-trips inside ``_build_summary_block``
   (``_actual_ctl_today`` + ``_hrv_trend_score`` both bypassed the
   ``cached()`` helper). With real ICU latency (300-500ms) this
   compounded to over a second of blocking work per /api/calendar call.

The test locks both:
- ``/api/calendar`` warm response < 250ms (was ~480ms).
- ``loadTodaySession()`` not awaited from ``loadHome()`` so its latency
  cannot block downstream cards.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = REPO_ROOT / "templates/dashboard.html"


@pytest.fixture(scope="module")
def client():
    from app import app  # local import: app boots with side-effects
    c = TestClient(app)
    # Warm everything once so ICU credentials check + library scan + plan
    # JSON load are out of the way before we measure.
    for ep in ("/api/calendar", "/api/today-session", "/api/wellness?days=90",
               "/api/wellness?days=14", "/api/wellness?days=7"):
        try:
            c.get(ep)
        except Exception:
            pass
    return c


def _time(client, endpoint, n=3):
    durs = []
    for _ in range(n):
        t0 = time.perf_counter()
        client.get(endpoint)
        durs.append((time.perf_counter() - t0) * 1000.0)
    return min(durs), sum(durs) / len(durs), max(durs)


# ── Server-side: /api/calendar must be fast. The bypass-cache fix in
#    _actual_ctl_today + _hrv_trend_score takes the warm path from ~480ms
#    to ~100ms. Test asserts it stays under 250ms warm.
def test_api_calendar_warm_under_250ms(client):
    _mn, avg, _mx = _time(client, "/api/calendar", n=3)
    assert avg < 250, (
        f"/api/calendar warm avg {avg:.0f}ms exceeds 250ms budget — "
        "did _actual_ctl_today / _hrv_trend_score bypass the cached() wrapper again?"
    )


# ── Server-side: /api/today-session is allowed to be slower (it does the
#    weekly-plan rebuild) but must not regress catastrophically. Lock it
#    under 1.0s warm — purely a tripwire so future changes that make it
#    >1s force a discussion about caching or paralleling.
def test_api_today_session_warm_under_1000ms(client):
    _mn, avg, _mx = _time(client, "/api/today-session", n=3)
    assert avg < 1000, (
        f"/api/today-session warm avg {avg:.0f}ms exceeds 1000ms tripwire — "
        "consider caching the weekly-plan rebuild or moving more work behind cached()."
    )


# ── Client-side: loadTodaySession() must NOT be awaited from loadHome().
#    Pre-fix this single await blocked every card painted below it
#    (recent-activities, body-perf-card, power-curve-chart,
#    readiness-composite-content) for 200-700ms warm and far worse cold.
def test_load_home_does_not_await_load_today_session():
    html = DASHBOARD_FILE.read_text(encoding="utf-8")

    # Find the body of loadHome().
    start = html.find("async function loadHome()")
    assert start >= 0, "loadHome() not found in dashboard.html"
    # Find the next top-level function definition to bound the search.
    end = html.find("\nasync function loadReadinessComposite", start + 1)
    assert end > start, "could not locate loadHome() end boundary"
    body = html[start:end]

    # Locate the loadTodaySession call site — there must be exactly one.
    assert "loadTodaySession()" in body, (
        "loadTodaySession() call missing from loadHome() — moved? renamed?"
    )

    # The call must NOT be preceded by `await` on the same line, since that
    # would re-introduce the blocking behaviour the v1.3.3 fix removed.
    for line in body.splitlines():
        if "loadTodaySession()" in line and "//" not in line.split("loadTodaySession()")[0]:
            stripped = line.strip()
            assert not stripped.startswith("await "), (
                "loadHome() awaits loadTodaySession() — that blocks every "
                "card painted below it. Drop the `await` so it runs alongside "
                "the other tail loaders (loadMorningLog, loadBodyPerf, etc.)."
            )
