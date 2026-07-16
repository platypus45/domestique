"""3.4.3 — opening the Training Plan tab auto-jumps the calendar to today.

Owner: the calendar spans months of past weeks; opening the tab landed at
the top and demanded a long manual scroll. The plan-tab open sequence now
calls calJumpToToday('auto') once rows exist; the header button keeps its
smooth scroll.
"""
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
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


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"


def test_jump_behavior_param_auto_vs_smooth():
    fn = _extract_js_function(SRC, "calJumpToToday")
    harness = """
let calls = [];
const document = { querySelector: (sel) => ({
  scrollIntoView: (opts) => calls.push(opts) }),
  getElementById: () => null };
const window = {};
""" + fn + """
calJumpToToday('auto');
calJumpToToday();
if (calls[0].behavior !== 'auto') throw new Error('tab-open jump must be instant, got ' + calls[0].behavior);
if (calls[1].behavior !== 'smooth') throw new Error('button jump must stay smooth, got ' + calls[1].behavior);
console.log('OK');
"""
    _run_node(harness)


def test_plan_tab_open_sequence_jumps_after_load():
    # The plan loader must call the jump AFTER runPlanOpenSequence resolves
    # (rows exist) and BEFORE the rest of the open-tab work.
    m = re.search(
        r"await runPlanOpenSequence\(\);.*?calJumpToToday\('auto'\);.*?loadPlanMetrics\(\);",
        SRC, re.S)
    assert m, "plan-tab open sequence must auto-jump to today after loading"
