"""v1.8.0 — frontend wiring text-scan tests.

Mirrors `test_v175_apply_tier_down.py::test_dashboard_has_apply_tier_down_handler`:
no DOM/playwright, just pin the strings the backend contract depends on so
a future edit can't silently drop them.

Surface area pinned by this file:
  - Readiness card source pill ("From HRV+TSB" / "From Hooper override" /
    "Insufficient signal") + the data-source attribute.
  - Severity-gated button rendering: id="apply-week-tier-down-btn",
    handler `applyWeekAutoAdjust` that POSTs to `/api/plan/auto-adjust`
    with `dry_run: true` (preview) then `dry_run: false` (commit).
  - The v1.7.5 single-day tier-down handler still exists and the gate
    now reads d.severity instead of the old score range.
  - Modal/Toast reuse: `openModal(` invoked from the new handler.
  - Calendar coloring: `_classifColor` helper + `pol-{polarized,
    pyramidal, threshold, hiit, base, unique}` CSS classes; the hiit
    pink `#ec4899` is the only new hex.
  - Data path: `actual.polarization.classification` reference in
    renderCalDay (main calendar) AND a polarization lookup inside
    loadWeeklyCalendar (weekly calendar).
"""
from __future__ import annotations

from pathlib import Path

import app as app_module


_DASH = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


# ─── Source pill ───────────────────────────────────────────────────────────

def test_dashboard_has_source_pill_class():
    # 65eaaec3 (issue #3 R2) unified the readiness surfaces into one card;
    # the standalone pill class became an inline srcPill span keyed on the
    # same backend `source` field. Assert the mechanism, not the old class.
    assert "srcPill" in _DASH


def test_dashboard_has_source_pill_labels():
    # Labels renamed in the unified card (same three sources).
    assert "From your leg-check" in _DASH      # hooper
    assert "From HRV + form" in _DASH           # tsb_hrv_auto
    assert "Limited data" in _DASH              # insufficient


def test_dashboard_source_pill_emits_data_source_attribute():
    # The unified card branches on the backend `source` values directly.
    assert "'tsb_hrv_auto'" in _DASH and "'hooper'" in _DASH and "'insufficient'" in _DASH


# ─── Severity-gated buttons ────────────────────────────────────────────────

def test_dashboard_has_week_auto_adjust_button_id():
    assert 'id="apply-week-tier-down-btn"' in _DASH


def test_dashboard_has_week_auto_adjust_handler():
    assert "function applyWeekAutoAdjust" in _DASH or "async function applyWeekAutoAdjust" in _DASH


def test_dashboard_keeps_v175_single_day_handler():
    # We must NOT touch the v1.7.5 button; it stays as the today-only path.
    assert "function applyReadinessTierDown" in _DASH or "async function applyReadinessTierDown" in _DASH
    assert 'id="apply-tier-down-btn"' in _DASH
    assert "/api/readiness/apply-tier-down" in _DASH


def test_dashboard_severity_gate_replaces_score_range_gate():
    # The new gate reads d.severity (catches Hooper override when score is null).
    assert "d.severity" in _DASH or "data.severity" in _DASH
    assert "sev === 'tier_down'" in _DASH
    assert "sev === 'rest'" in _DASH


def test_dashboard_has_rest_button_label():
    # severity=rest collapses to a single button labeled "Apply rest day".
    assert "Apply rest day" in _DASH


def test_dashboard_has_auto_adjust_week_button_label():
    assert "Auto-adjust this week" in _DASH


# ─── Auto-adjust flow (dry-run preview → apply) ────────────────────────────

def test_dashboard_posts_auto_adjust_endpoint():
    assert "/api/plan/auto-adjust" in _DASH


def test_dashboard_auto_adjust_uses_dry_run_true_preview():
    # Preview pass MUST send dry_run:true so backend does not persist.
    assert "dry_run: true" in _DASH or "dry_run:true" in _DASH


def test_dashboard_auto_adjust_uses_dry_run_false_on_apply():
    # Commit pass MUST send dry_run:false.
    assert "dry_run: false" in _DASH or "dry_run:false" in _DASH


def test_dashboard_auto_adjust_opens_modal_for_preview():
    # The handler must call openModal(...) to render the preview table.
    # We grep for the substring inside the handler block.
    start = _DASH.find("function applyWeekAutoAdjust")
    if start < 0:
        start = _DASH.find("async function applyWeekAutoAdjust")
    assert start >= 0, "applyWeekAutoAdjust handler not found"
    next_fn = _DASH.find("\nasync function ", start + 1)
    if next_fn < 0:
        next_fn = _DASH.find("\nfunction ", start + 1)
    chunk = _DASH[start:next_fn] if next_fn > 0 else _DASH[start:]
    assert "openModal(" in chunk, "applyWeekAutoAdjust must call openModal() for the preview"


def test_dashboard_auto_adjust_has_apply_and_cancel_buttons():
    # Modal must offer both an Apply button and a Cancel button.
    assert "auto-adjust-apply-btn" in _DASH
    assert "closeModal()" in _DASH
    assert ">Cancel<" in _DASH
    assert ">Apply<" in _DASH


def test_dashboard_auto_adjust_commit_helper_exists():
    assert "_applyWeekAutoAdjustCommit" in _DASH


def test_dashboard_auto_adjust_zwo_cleared_note():
    # Per addendum §F1: actions with zwo_cleared:true must be surfaced in
    # the preview so the user knows the workout link was dropped.
    assert "zwo_cleared" in _DASH


# ─── Calendar coloring ─────────────────────────────────────────────────────

def test_dashboard_has_classif_color_helper():
    assert "function _classifColor" in _DASH


def test_dashboard_classif_color_uses_correct_css_vars():
    # The exact color map locked by MASTER_DECISIONS_v180.md.
    start = _DASH.find("function _classifColor")
    assert start >= 0
    end = _DASH.find("\n}", start)
    block = _DASH[start:end + 2]
    assert "polarized: 'var(--red)'" in block
    assert "pyramidal: 'var(--orange)'" in block
    assert "threshold: 'var(--yellow)'" in block
    assert "hiit: '#ec4899'" in block
    assert "base: 'var(--green)'" in block
    assert "unique: 'var(--text3)'" in block


def test_dashboard_has_pol_css_classes():
    # The CSS classes serve both as a hook for the inline border-color and
    # as a diagnostic handle for tests/smoke runs.
    assert ".cal-actual.pol-polarized" in _DASH
    assert ".cal-actual.pol-pyramidal" in _DASH
    assert ".cal-actual.pol-threshold" in _DASH
    assert ".cal-actual.pol-hiit" in _DASH
    assert ".cal-actual.pol-base" in _DASH
    assert ".cal-actual.pol-unique" in _DASH


def test_dashboard_pol_hiit_uses_pink_hex():
    # Hard rule: hiit pink #ec4899 is the only new color — inline hex.
    assert "#ec4899" in _DASH


def test_dashboard_main_calendar_reads_polarization_classification():
    # renderCalDay must pull classification off the actual payload.
    # Be permissive about whitespace.
    assert "actual.polarization" in _DASH


def test_dashboard_weekly_calendar_reads_polarization_classification():
    # loadWeeklyCalendar walks dayActs looking for the same field; this
    # checks the helper invocation in BOTH render paths.
    # The main calendar uses `actual.polarization`; the weekly calendar
    # uses `a.polarization` over dayActs. _classifColor must be called.
    assert "_classifColor(" in _DASH
    # Two distinct call sites (one in renderCalDay, one in loadWeeklyCalendar).
    assert _DASH.count("_classifColor(") >= 2
