"""#34 — the Plan tab says which workouts were moved, and Settings says which
workouts Domestique kept where the rider moved them in intervals.icu.

Runs the real functions from dashboard.html under node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _fn(name: str) -> str:
    start = SRC.find(f"async function {name}")
    if start < 0:
        start = SRC.index(f"function {name}")
    i = SRC.index("{", start)
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:j + 1]
    raise AssertionError(name)


def _const(name: str) -> str:
    i = SRC.index(f"const {name}")
    return SRC[i:SRC.index("};", i) + 2]


def _node(js: str):
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


_ESC = ("const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
        ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');")
_CARD = "\n".join([_const("CAL_CONTENT_LABEL"), _const("CAL_SESSION_LABEL"),
                   _const("CAL_SESSION_CSS"), _ESC, _fn("calContentCss"), _fn("calCardTitle"),
                   _fn("calStructureSuffix"), _fn("calCardTitleWithStructure"),
                   _fn("plannedCardParts")])


@needs_node
def test_a_moved_session_says_so_on_its_card():
    out = _node(_CARD + """
const base = {session_type: 'z2', duration_min: 60, tss: 45, name: 'Z2 Endurance'};
console.log(JSON.stringify({
  moved: plannedCardParts(Object.assign({user_moved: true}, base), 'planned').metaText,
  plain: plannedCardParts(base, 'planned').metaText,
}));""")
    assert out["plain"] == "60m · 45 TSS"
    assert out["moved"].startswith("60m · 45 TSS · <span class=\"card-moved\"")
    assert ">moved</span>" in out["moved"] and "keeps its length" in out["moved"]


def test_the_plan_grid_passes_the_moved_flag_to_the_card():
    grid_call = SRC[SRC.index("const parts = plannedCardParts({"):]
    grid_call = grid_call[:grid_call.index("}, cardState);")]
    assert "user_moved: !!s.user_moved" in grid_call


@needs_node
@pytest.mark.parametrize("held,sync,expect", [
    (1, True, "Last update: 1 workout kept where you moved it in intervals.icu."),
    (3, True, "Last update: 3 workouts kept where you moved them in intervals.icu."),
    (0, True, None),
    (2, False, None),
])
def test_settings_shows_the_workouts_kept_in_intervals_icu(held, sync, expect):
    out = _node("""
const els = {};
function mk(id) { els[id] = {id, style: {display: ''}, textContent: '', innerHTML: '', checked: false, disabled: false}; return els[id]; }
['icu-cal-sync-block','icu-cal-sync-toggle','icu-cal-sync-hint','icu-cal-sync-held'].forEach(mk);
const $ = id => els[id] || null;
async function fetch() { return { json: async () => (%s) }; }
%s
icuCalSyncRefresh().then(() => console.log(JSON.stringify({
  text: els['icu-cal-sync-held'].textContent, shown: els['icu-cal-sync-held'].style.display })));
""" % (json.dumps({"connected": True, "write_ok": True, "sync_enabled": sync,
                   "last_result": {"held": held}}),
       # 3.14.0: the held line is worded by icuHeldLine (workouts and own rides).
       _fn("icuHeldLine") + "\n" + _fn("icuCalSyncRefresh")))
    if expect is None:
        assert out["shown"] == "none"
    else:
        assert out["shown"] == "block" and out["text"].startswith(expect), out


def test_settings_never_shows_another_profiles_push_result(monkeypatch):
    """The last push result is global; after a profile switch Settings showed
    the previous rider's count (review of #34)."""
    import types
    import app as app_module
    import profile_manager
    monkeypatch.setattr(app_module, "_icu_push_last_result",
                        {"at": "x", "profile_id": "anna", "held": 2})
    for active, expect in (("anna", 2), ("ben", None)):
        monkeypatch.setattr(profile_manager.ProfileManager, "get",
                            classmethod(lambda cls, a=active: types.SimpleNamespace(active_id=a)))
        got = app_module._last_result_for_active_profile()
        assert (got or {}).get("held") == expect, (active, got)
