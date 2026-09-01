"""Day-modal TSS honesty — the stat shows the matched FILE's TSS.

Owner case: the planner slot budgeted 94 TSS but the matched library file is
~75 TSS; the modal's TSS stat showed 94 — the number the rider would NOT
ride. The fix mirrors the v2.2.12 Duration decision: the stat is the FILE's
TSS (``session.zwo_tss``, enriched server-side from the library index row),
and the planner's slot estimate is demoted to a small labeled secondary
("plan slot: N TSS") shown only when |slot - file| > 10.

Node harness style follows tests/test_344_plan_grid_parity.py: extract the
REAL ``dayModalTssStat`` from templates/dashboard.html and run it under node
against fixtures.
"""
import json
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _run_node(harness: str) -> str:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"
    return res.stdout


def _render(session: dict) -> str:
    harness = (
        _extract_js_function(SRC, "dayModalTssStat") + "\n"
        + f"const out = dayModalTssStat({json.dumps(session)});\n"
        + "console.log(JSON.stringify(out));\n"
    )
    return json.loads(_run_node(harness))


def test_file_tss_leads_slot_demoted_on_gap():
    """75-vs-94: the stat is the file's 75; the 94 slot budget appears only
    as the small labeled secondary."""
    out = _render({"zwo_tss": 75, "tss_estimate": 94})
    assert '<div class="mv">75</div>' in out          # stat = FILE TSS
    assert "plan slot: 94 TSS" in out                 # slot demoted + labeled
    assert '<div class="mv">94</div>' not in out      # slot never the headline


def test_no_secondary_when_slot_close_to_file():
    """|slot - file| <= 10: one number, no secondary line."""
    out = _render({"zwo_tss": 75, "tss_estimate": 80})
    assert '<div class="mv">75</div>' in out
    assert "plan slot" not in out


def test_fileless_falls_back_to_slot_estimate():
    """No matched file (adjusted/live or missing_workout): slot estimate is
    the only number; no secondary."""
    out = _render({"zwo_tss": 0, "tss_estimate": 94})
    assert '<div class="mv">94</div>' in out
    assert "plan slot" not in out
    # Absent field entirely (older payloads) behaves the same.
    out2 = _render({"tss_estimate": 60})
    assert '<div class="mv">60</div>' in out2
    assert "plan slot" not in out2


def test_file_tss_rounded():
    """Index TSS is fractional (e.g. 74.6) — the stat shows a whole number."""
    out = _render({"zwo_tss": 74.6, "tss_estimate": 94})
    assert '<div class="mv">75</div>' in out
    assert "plan slot: 94 TSS" in out


def test_modal_body_uses_helper():
    """The modal's stats grid must render TSS through dayModalTssStat — the
    raw ``session.tss_estimate`` metric must not come back."""
    assert "html += dayModalTssStat(session);" in SRC
    assert (
        '<div class="metric"><div class="mv">${session.tss_estimate}</div>'
        '<div class="ml">TSS</div></div>' not in SRC
    )
