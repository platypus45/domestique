"""The On-track card renders what /api/calendar sends.

The server's intensity split has been three zones (z1_pct / z2_pct / z3_pct)
since e14fb3b5 (2026-09-10); the card read z1z2_pct / z3_pct / z4plus_pct and
showed "Polarized undefined/9/undefined" on the owner's dashboard (reported
2026-09-14). No test rendered the card from the server's payload. This one does,
under node, and refuses "undefined" or "NaN" anywhere in it -- with CTL known,
and with CTL unknown (possible since fitness.state(), 070ad407)."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cache
import clock

SRC = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


def _fn(name):
    start = SRC.index(f"function {name}(")
    i = SRC.index("{", start)
    depth = 0
    for j in range(i, len(SRC)):
        depth += {"{": 1, "}": -1}.get(SRC[j], 0)
        if depth == 0:
            return SRC[start:j + 1]


def _calendar(icu):
    today = date(2026, 9, 16)
    clock.freeze(today)
    tmp = Path(tempfile.mkdtemp())
    mon = today - timedelta(days=today.weekday())
    sess = [{"day": (mon + timedelta(days=i)).isoformat(), "session_type": "z2", "duration_min": 60,
             "tss_estimate": 45, "zwo_file": "endurance_steady_74pct_70min.zwo", "status": "pending"}
            for i in range(7)]
    plan = {"goal": {"type": "general", "distribution": "polarized"}, "phases": [],
            "weeks": [{"week_num": 1, "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                       "phase": "base", "tss_target": 315, "is_stepback": False, "sessions": sess}],
            "ctl_snapshot": {"current_ctl": 40.0, "generated_on": mon.isoformat()}}
    (tmp / "current_plan.json").write_text(json.dumps(plan))
    ride = {"started_at": f"{(today - timedelta(days=1)).isoformat()}T08:00:00", "tss": 60, "sport": "Ride",
            "time_in_zone": {"z1": 600, "z2": 1800, "z3": 600, "z4": 300, "z5": 200}}
    try:
        with patch.object(app_module, "_plan_dir", return_value=tmp), \
             patch.object(app_module, "_kick_lazy_icu_sync", return_value=False), \
             patch.object(app_module, "_load_all_rides_safe", return_value=[ride]), \
             patch.object(app_module, "get_today_metrics", return_value=icu), \
             patch("fitness._cached_row", return_value=None):
            cache.clear_cache()
            return TestClient(app_module.app).get("/api/calendar").json()
    finally:
        clock.unfreeze()
        cache.clear_cache()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("icu", [{"ctl": 42.0, "atl": 50.0}, {}], ids=["ctl-known", "ctl-unknown"])
def test_the_ontrack_card_has_no_undefined(icu):
    data = _calendar(icu)
    assert set(data["summary"]["polarized_actual"]) == {"z1_pct", "z2_pct", "z3_pct"}
    harness = "\n".join([
        "const host = { innerHTML: '' };",
        "const document = { getElementById: id => id === 'ontrack-bar-host' ? host : null };",
        "const esc = s => String(s == null ? '' : s);",
        _fn("mondayOf"),
        _fn("renderOnTrackBar"),
        f"renderOnTrackBar({json.dumps(data)});",
        "console.log(JSON.stringify(host.innerHTML));",
    ])
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr[-1500:]
    html = json.loads(res.stdout)
    text = html.replace("undefined-", "")  # no legitimate token contains it; keep the check strict
    assert "undefined" not in text and "NaN" not in text, html
    pa, pt = data["summary"]["polarized_actual"], data["summary"]["polarized_target"]
    assert f"{pa['z1_pct']}/{pa['z2_pct']}/{pa['z3_pct']}" in html
    assert f"target {pt['z1_pct']}/{pt['z2_pct']}/{pt['z3_pct']}" in html
    if not icu:
        assert "no CTL on record" in html
