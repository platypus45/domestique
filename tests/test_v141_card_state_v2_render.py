"""v1.4.1 — renderCalDay reads card_state_v2 with 6 finer-grained variant
classes layered on top of the legacy 4-state.

Asserted contract:
- Server emits both `card_state` (legacy 4) and `card_state_v2` (10) on
  every session (since v1.4.0; see test_v140_session_fields_contract).
- `renderCalDay` (templates/dashboard.html) reads `d.card_state_v2` first,
  then falls back to `d.card_state`.
- 6 of the 10 v2 states map to a CSS variant class:
    past_planned_no_ride → cal-day-past-skipped       (red tint)
    past_actual_only     → cal-day-past-unplanned     (purple tint)
    past_planned_actual  → cal-day-past-completed     (green tint)
    today_planned        → cal-day-today-planned      (blue tint)
    today_actual         → cal-day-today-completed    (green strong)
    future_unavailable   → cal-day-future-unavailable (gray tint)
- The remaining 4 states (past_no_ride, future_planned, future_rest,
  missing_workout) keep their legacy short-circuit rendering.
- Each variant CSS rule exists with the documented background tint.
- The cell carries `data-cs-v2="<state>"` so future panels can dispatch
  on it.

These are static-asset tests — they read the template text rather than
spin up a browser. That's enough because the JS is plain string
concatenation and the CSS is a flat selector list.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


DASHBOARD = ROOT / "src" / "templates" / "dashboard.html"
HTML = DASHBOARD.read_text(encoding="utf-8")


def test_render_reads_card_state_v2_with_legacy_fallback():
    """`renderCalDay` reads `d.card_state_v2` and falls back to `d.card_state`."""
    # The exact line we added in v1.4.1.
    assert "d.card_state_v2 || cs" in HTML, (
        "renderCalDay must read card_state_v2 with `cs` (legacy) as fallback"
    )


def test_v2_class_map_includes_all_six_variants():
    """The JS dispatch table maps the 6 finer states to their CSS classes."""
    # Look for the inline object literal we added in renderCalDay.
    expected_pairs = [
        ("past_planned_no_ride", "cal-day-past-skipped"),
        ("past_actual_only", "cal-day-past-unplanned"),
        ("past_planned_actual", "cal-day-past-completed"),
        ("today_planned", "cal-day-today-planned"),
        ("today_actual", "cal-day-today-completed"),
        ("future_unavailable", "cal-day-future-unavailable"),
    ]
    for state, cls in expected_pairs:
        assert f"{state}:" in HTML, f"v2Cls map missing entry for {state}"
        assert cls in HTML, f"v2Cls map missing CSS class {cls}"


def test_css_rules_for_six_variants_exist():
    """Each variant class has a CSS rule with a background tint."""
    rules = [
        # (selector, color-fragment-that-must-be-present)
        (".cal-day.cal-day-past-skipped", "239, 68, 68"),     # red
        (".cal-day.cal-day-past-unplanned", "168, 85, 247"),  # purple
        (".cal-day.cal-day-past-completed", "34, 197, 94"),   # green
        (".cal-day.cal-day-today-planned", "59, 130, 246"),   # blue
        (".cal-day.cal-day-today-completed", "34, 197, 94"),  # green strong
        (".cal-day.cal-day-future-unavailable", "100, 116, 139"),  # gray
    ]
    for sel, color in rules:
        # Match the selector followed by an opening brace within ~200 chars,
        # then the color triple.
        rule_pattern = re.compile(
            re.escape(sel) + r"[^}]*" + re.escape(color),
            re.DOTALL,
        )
        assert rule_pattern.search(HTML), (
            f"CSS rule for {sel} with color rgba({color}) is missing"
        )


def test_cell_carries_data_cs_v2_attribute():
    """The rendered cell has `data-cs-v2="<state>"` so other code can read it."""
    assert 'data-cs-v2="${esc(csV2)}"' in HTML, (
        "cell template must include data-cs-v2 attribute"
    )


def test_v2_class_is_appended_not_replacing_legacy():
    """v2Cls is APPENDED to stateCls; legacy classes (cal-completed,
    cal-missing, cal-rest) still apply via the existing render paths."""
    assert "stateCls += v2Cls;" in HTML, (
        "v2 class must be appended; legacy stateCls (cal-completed/missing) "
        "must still apply"
    )
    # And cal-completed / cal-missing rules still present (untouched).
    assert ".cal-day.cal-completed { background: rgba(34,197,94,0.05); }" in HTML
    assert ".cal-day.cal-missing  { background: rgba(239,68,68,0.06); }" in HTML


def test_unmapped_v2_states_emit_empty_class():
    """The 4 v2 states without a v2 class entry (past_no_ride, future_planned,
    future_rest, missing_workout) fall through to `''` in the dispatch."""
    # These states keep their legacy rendering; the dispatch shouldn't list them.
    js_block_match = re.search(
        r"const v2Cls = \(\{[^}]+\}\[csV2\] \|\| ''\);",
        HTML,
        re.DOTALL,
    )
    assert js_block_match, "v2Cls dispatch block not found in expected shape"
    block = js_block_match.group(0)
    for unmapped in ("past_no_ride", "future_planned", "future_rest",
                     "missing_workout"):
        assert f"{unmapped}:" not in block, (
            f"{unmapped} should NOT be in v2Cls map (legacy short-circuit "
            f"handles it)"
        )
