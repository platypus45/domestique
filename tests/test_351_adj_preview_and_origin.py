"""v3.5.1 — two fixes around the day-modal adjustment banner.

1. Origin gate: the CSRF Origin check must allow ANY localhost/127.0.0.1
   port. The old allow-list pinned :8080, so serving anywhere else (dev
   preview on :8090, a stray uvicorn on :8000) returned 403 for EVERY
   mutating POST — the UI's buttons looked dead ("Ride it anyway" did
   nothing; a later press in the packaged app then worked).

2. Preview-before-revert: the collapsed "Original plan" block in the
   adjusted day modal lazily renders the original file's real segment
   chart on expand, so the rider can see what "Ride the original anyway"
   restores before pressing it. Structural pins + node behaviour tests.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")


# ── 1. Origin gate ───────────────────────────────────────────────────────────

def test_allowed_origins_are_port_agnostic_localhost():
    import app as appmod
    prefixes = appmod._ALLOWED_ORIGIN_PREFIXES
    # Port-agnostic: the prefix ends at the colon, so any port matches.
    assert "http://localhost:" in prefixes
    assert "http://127.0.0.1:" in prefixes
    # Regression shape: no prefix may pin a port (the :8080 bug).
    for p in prefixes:
        assert not re.search(r":\d", p), f"port-pinned origin prefix: {p!r}"


def test_origin_check_blocks_cross_site_allows_any_local_port():
    from fastapi.testclient import TestClient
    import app as appmod
    client = TestClient(appmod.app)
    # Cross-site origin still 403s (the actual CSRF the check exists for).
    r = client.post("/api/readiness/revert-cap", json={},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # Any localhost port passes the gate (may hit the endpoint's own logic,
    # but must NOT be the middleware's 403 "origin not allowed").
    for origin in ("http://localhost:8090", "http://127.0.0.1:8000"):
        r = client.post("/api/readiness/revert-cap", json={},
                        headers={"Origin": origin})
        assert r.status_code != 403, origin


# ── 2. Preview-before-revert (structure) ─────────────────────────────────────

def test_original_block_carries_lazy_preview_hooks():
    block = DASH[DASH.index("function _adjOriginalBlockHtml"):]
    block = block[:block.index("\n}") + 2]
    # <details> wires the lazy loader; chart container carries the file.
    assert 'ontoggle="_loadAdjOrigPreview(this)"' in block
    assert 'class="adj-orig-chart"' in block
    assert "data-zwo=" in block
    assert "open to preview" in block


def test_loader_exists_uses_real_segment_pipeline_and_is_one_shot():
    fn = DASH[DASH.index("async function _loadAdjOrigPreview"):]
    fn = fn[:fn.index("\n}") + 2]
    # Same segment source + renderer as the modal lead chart.
    assert "/api/workout/all/" in fn
    assert "workoutProfileSVG(" in fn
    # One-shot guard and quiet failure.
    assert "dataset.loaded" in fn
    assert "preview unavailable" in fn


# ── 2b. Preview-before-revert (behaviour, node harness) ──────────────────────

def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}")
    j = src.index("\n}", i) + 2
    # include the `async ` prefix when present
    k = src.rfind("\n", 0, i) + 1
    return src[k:j]


@pytest.mark.skipif(subprocess.run(["which", "node"], capture_output=True).returncode != 0,
                    reason="node not installed")
def test_loader_behaviour_lazy_once_and_failure_path():
    fn = _extract_js_function(DASH, "_loadAdjOrigPreview")
    harness = fn + """
const calls = [];
global.fetch = async (url) => { calls.push(url);
  return {ok: true, json: async () => ({segments: [{type:'SteadyState',duration:600,power:1.0}], ftp: 250, total_seconds: 600})}; };
global.workoutProfileSVG = () => '<svg data-test="orig"></svg>';
global.settingsFtp = 250;
function mkDet(zwo) {
  const box = {dataset: {zwo}, innerHTML: ''};
  return {open: true, box, querySelector: (sel) => sel === '.adj-orig-chart' ? box : null};
}
(async () => {
  // closed details -> no fetch
  const closed = mkDet('x.zwo'); closed.open = false;
  await _loadAdjOrigPreview(closed);
  if (calls.length !== 0) throw new Error('fetched while closed');
  // open -> exactly one fetch, chart + caption injected
  const det = mkDet('Threshold 2x15min.zwo');
  await _loadAdjOrigPreview(det);
  if (calls.length !== 1) throw new Error('expected 1 fetch, got ' + calls.length);
  if (!/data-test="orig"/.test(det.box.innerHTML)) throw new Error('no chart injected');
  if (!/restores for today/.test(det.box.innerHTML)) throw new Error('no caption');
  // second toggle -> still one fetch (one-shot)
  await _loadAdjOrigPreview(det);
  if (calls.length !== 1) throw new Error('re-fetched on second toggle');
  // failure path -> quiet caption, no throw
  global.fetch = async () => ({ok: false, status: 500});
  const det2 = mkDet('gone.zwo');
  await _loadAdjOrigPreview(det2);
  if (!/preview unavailable/.test(det2.box.innerHTML)) throw new Error('no failure caption');
  console.log('OK');
})().catch(e => { console.error(e.message); process.exit(1); });
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"
    assert "OK" in res.stdout
