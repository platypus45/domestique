"""The dashboard reads one object per question (week-view contract A10).

Until 2026-09-14 a home load fetched /api/readiness three times and
/api/activities, /api/settings and /api/icu/connection twice each (measured
over the Chrome DevTools protocol against a sandboxed server), the Activities
and Duration tiles counted rides over a window of their own, the weekly rollup
fell back to rebuilding /api/week-summary in the browser, the Plan tab's week
Actual read a snapshot the last reforecast stored, and two painters took turns
on the This Week strip: the calendar's on a home load, the weekly-plan one
after a tier-down, a sync or a rematch."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import app as app_module

SRC = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = SRC.find(f"async function {name}(")
    if start < 0:
        start = SRC.index(f"function {name}(")
    i = SRC.index("{", start)
    depth = 0
    for j in range(i, len(SRC)):
        depth += {"{": 1, "}": -1}.get(SRC[j], 0)
        if depth == 0:
            return SRC[start:j + 1]
    raise AssertionError(name)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_one_request_while_in_flight_and_never_a_stale_one():
    harness = _fn("getResource") + r"""
const window = {};
let calls = 0, release = [];
function fetch(path) {
  calls++;
  return new Promise(res => release.push(() => {
    let body = JSON.stringify({ path, n: calls });
    const mk = () => ({ ok: true, clone: mk, json: async () => JSON.parse(body),
                        arrayBuffer: async () => new ArrayBuffer(body.length) });
    res(mk());
  }));
}
(async () => {
  const a = getResource('/api/readiness'), b = getResource('/api/readiness');
  const c = getResource('/api/settings');
  if (calls !== 2) throw new Error('concurrent calls must share one request per path, got ' + calls);
  release.splice(0).forEach(f => f());
  const [ra, rb] = await Promise.all([a, b]);
  if ((await ra.json()).n !== (await rb.json()).n) throw new Error('both callers read the one response');
  await c; await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  const d = getResource('/api/readiness');
  if (calls !== 3) throw new Error('a finished response must never be served again, got ' + calls);
  release.splice(0).forEach(f => f()); await d;
  console.log('OK');
})().catch(e => { console.error(e.message); process.exit(1); });
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_refresh_after_a_write_does_not_join_an_earlier_read():
    """Any completed POST forgets the shared reads: the home load's sync
    repaint, a tier-down, a move -- every write, through one choke point."""
    harness = _fn("getResource") + _fn("forgetInflight") + r"""
let calls = 0, finishPost;
const window = {
  fetch(path, init) {
    if (init && init.method === 'POST') return new Promise(res => { finishPost = () => res({ ok: true }); });
    calls++; return new Promise(() => {});                        // a read still in flight
  },
};
const fetch = (...a) => window.fetch(...a);
(async () => {
  getResource('/api/today-session');                               // installs the choke point
  getResource('/api/today-session');
  if (calls !== 1) throw new Error('concurrent reads share one request, got ' + calls);
  const post = window.fetch('/api/rides/sync', { method: 'POST' });
  getResource('/api/today-session');
  if (calls !== 1) throw new Error('a read during the write still shares, got ' + calls);
  finishPost(); await post; await new Promise(r => setTimeout(r, 0));
  getResource('/api/today-session');                               // the refresh after the write
  if (calls !== 2) throw new Error('the refresh joined a read from before the write');
  console.log('OK');
})().catch(e => { console.error(e.message); process.exit(1); });
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr


def test_the_home_reads_go_through_it():
    plain = re.findall(r"\bfetch\('(/api/(?:readiness|activities|settings|icu/connection|"
                       r"week-summary[^']*|calendar|weekly-plan|today-session))'\)", SRC)
    assert plain == []


def test_the_client_derivations_are_gone():
    for gone in ("_buildWeekSummaryFallback", "lastWeekActs", "thisWeekActs", "_dayAggregate"):
        assert gone not in SRC, gone


def test_one_painter_for_the_this_week_strip():
    body = _fn("loadWeeklyCalendar")
    assert "weekly-calendar').innerHTML = html" not in body
    assert "renderThisWeekFromCalendar(" in body
    painters = re.findall(r"getElementById\('weekly-calendar'\)|\$\('weekly-calendar'\)\.innerHTML = (?!'<div style=\"text-align:center)", SRC)
    assert len(painters) == 1, painters
