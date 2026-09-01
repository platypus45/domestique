"""v3.5.6 — plan-configurator foldout + the off-screen calendar auto-jump.

Two owner reports:

1. "Sometimes when I reopen the app I can't click the training in the training
   plan. Then the training 2 rows above my mouse becomes dark and opens. Then I
   scroll the page and I can open today's training."
   Measured cause: calJumpToToday('auto') scrolls the INNER #cal-body container
   (~1435px) while that container is still BELOW THE FOLD on a fresh plan-tab
   open (page scrollY 0, container top ~y=1435 in an 860px window). WebKit does
   not reliably re-map hit-test regions for an overflow scroller it scrolled
   while off-screen, so rows paint scrolled while clicks resolve against the
   pre-scroll geometry. The manual page scroll is what invalidates it.
   Fix: defer the auto-jump until the container is actually on screen, then
   force one layout flush. The header button is never deferred.

2. "The PLAN CONFIGURATOR — make it foldout, really clear you can fold it out,
   folded out by default when no plan exists, folded in when you have one. But
   not just with a small arrow."
   Fix: a full-width bar whose right-hand hint names the action, defaulting
   open/closed on has-plan, with an explicit user toggle persisted per profile.
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """Extract a top-level JS function body by name."""
    i = DASH.index(f"function {name}")
    return DASH[i:DASH.index("\n}", i) + 2]


# ── 1. calendar auto-jump is gated on visibility ─────────────────────────────

def test_auto_jump_is_gated_on_container_visibility():
    body = _fn("calJumpToToday")
    assert "_calJumpWhenVisible(scroller, behavior)" in body, (
        "the auto-jump must be gated on the scroller being on screen")
    # The gate must run BEFORE the scroll is computed/applied.
    assert body.index("_calJumpWhenVisible") < body.index("scrollTo"), (
        "visibility gate must precede the scrollTo")


def test_only_the_auto_jump_defers_never_the_header_button():
    gate = _fn("_calJumpWhenVisible")
    assert "if (behavior !== 'auto') return true;" in gate, (
        "an explicit user click must never be deferred")


def test_gate_uses_viewport_intersection_not_a_bare_offset_check():
    gate = _fn("_calJumpWhenVisible")
    # Real visibility test: rect against the viewport, both edges.
    assert "getBoundingClientRect" in gate
    assert "r.top < vh" in gate and "r.bottom > 0" in gate
    # Must not spin forever if the user never scrolls down.
    assert "IntersectionObserver" in gate
    assert "__calJumpPending" in gate, "must not stack duplicate waiters"
    assert re.search(r"tries\s*>\s*\d+", gate), "rAF fallback needs a bound"


def test_programmatic_scroll_forces_a_layout_flush():
    body = _fn("calJumpToToday")
    assert "void scroller.offsetHeight" in body, (
        "a layout read is what re-maps stale hit-test regions after the scroll")


# ── 2. plan-configurator foldout ─────────────────────────────────────────────

def test_configurator_body_is_wrapped_and_collapsible():
    assert 'id="plan-config-body"' in DASH
    assert 'id="plan-config-header"' in DASH
    # The Generate button must live INSIDE the collapsible body, or collapsing
    # would hide nothing that matters / leave the primary action orphaned.
    body_start = DASH.index('id="plan-config-body"')
    body_end = DASH.index("<!-- /#plan-config-body -->")
    assert body_start < DASH.index('id="btn-generate-plan"') < body_end


def test_affordance_is_a_full_bar_with_a_named_action_not_a_bare_arrow():
    """The owner's explicit ask: not just a small arrow."""
    assert ".planfold-bar" in DASH
    bar = DASH[DASH.index(".planfold-bar {"):DASH.index(".cal-scroll {")]
    assert "cursor: pointer" in bar
    assert "border:" in bar and "background:" in bar   # visibly a control
    assert ".planfold-bar:hover" in bar                # reacts to the mouse
    # The hint text must name what happens, not just show state.
    setter = _fn("_planCfgSet")
    assert "Change settings / regenerate" in setter
    assert "Hide" in setter


def test_default_is_open_with_no_plan_and_closed_with_a_plan():
    fn = _fn("planConfigApplyDefault")
    assert "_planCfgSet(!hasPlan)" in fn, "default must key off plan existence"


def test_explicit_user_toggle_wins_and_persists_per_profile():
    fn = _fn("planConfigApplyDefault")
    assert "_lsGet(_profileLsKey('planCfgOpen'))" in fn
    assert "stored === '1'" in fn and "stored === '0'" in fn, (
        "a stored preference must override the has-plan default")
    tog = _fn("togglePlanConfig")
    assert "_lsSet(_profileLsKey('planCfgOpen')" in tog, "toggle must persist"


def test_loader_applies_the_default_exactly_once():
    assert DASH.count("planConfigApplyDefault(!!d.plan_json)") == 1, (
        "one call site only — loadPlan owns the exists/doesn't decision")


def test_aria_state_tracks_the_fold():
    setter = _fn("_planCfgSet")
    assert "aria-expanded" in setter
    assert 'aria-controls="plan-config-body"' in DASH
