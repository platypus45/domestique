"""v1.3.2 UX hot-fix — explicit UPDATE button replaces auto-save in the
availability calendar.

Pre-fix (v1.3.1): editing any availability field POSTed via a 500 ms
debounce. The reflow ran while the user was still mid-edit, surprising
them with shifting plan values.

Post-fix (v1.3.2): edits flip a local ``_availDirty`` flag, the in-page
``#avail-update-btn`` pulses the accent color, and the POST only fires
when the user clicks UPDATE. An ``aria-live`` region surfaces
``sessions_modified`` for assistive tech and auto-fades after 4 s.

These tests assert the dashboard markup carries the expected hooks. They
do NOT exercise the JS runtime (would require a headless browser).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = REPO_ROOT / "src" / "templates" / "dashboard.html"


def _read_dashboard() -> str:
    return DASHBOARD_FILE.read_text()


def test_popover_renders_update_button_in_idle_state():
    """Availability calendar must render an UPDATE button. On first paint
    (no edits made) the button reads 'UP TO DATE' — the idle label — and
    is NOT decorated with the .dirty pulsing class."""
    html = _read_dashboard()

    # Button element with the canonical id.
    button_match = re.search(
        r'<button[^>]*id="avail-update-btn"[^>]*>([^<]*)</button>',
        html,
    )
    assert button_match is not None, (
        "missing <button id='avail-update-btn'> in dashboard.html — "
        "v1.3.2 introduces an explicit UPDATE button on the Availability Calendar"
    )

    # Idle label.
    idle_label = button_match.group(1).strip()
    assert idle_label == "UP TO DATE", (
        f"expected idle button label 'UP TO DATE', got {idle_label!r}"
    )

    # No 'dirty' class baked into the markup — that class is added by JS
    # only when _availDirty is true.
    button_open_tag = button_match.group(0).split(">", 1)[0]
    assert "dirty" not in button_open_tag, (
        "UPDATE button should not start in dirty state — class 'dirty' "
        "must be applied dynamically by JS, not in the rendered HTML"
    )

    # Button wires onclick to the commit function.
    assert "onclick=\"_commitAvailUpdate()\"" in button_open_tag, (
        "UPDATE button must call _commitAvailUpdate() on click"
    )


def test_editing_a_field_toggles_dirty_class_on_update_button():
    """The JS pipeline that propagates field edits to the UPDATE button
    must exist: _saveAvailability() (called by every per-day mutator)
    sets _availDirty=true and calls _renderAvailUpdateBtn(), which adds
    the .dirty class to #avail-update-btn."""
    html = _read_dashboard()

    # _saveAvailability flips dirty + repaints the button (no longer
    # debounce-POSTs to /api/plan/save-availability).
    save_avail_match = re.search(
        r"function\s+_saveAvailability\s*\(\s*\)\s*\{([^}]+)\}",
        html,
    )
    assert save_avail_match is not None, "missing function _saveAvailability()"
    save_body = save_avail_match.group(1)
    assert "_availDirty = true" in save_body, (
        "_saveAvailability() must set _availDirty=true so per-day edits "
        "(via _setAvailDay / _toggleUnavailable / markDayUnavailable) "
        "light up the UPDATE button"
    )
    assert "_renderAvailUpdateBtn" in save_body, (
        "_saveAvailability() must call _renderAvailUpdateBtn() to repaint "
        "the UPDATE button after a local edit"
    )
    # Critically: it must NOT POST. v1.3.1's auto-save is gone.
    assert "fetch(" not in save_body, (
        "v1.3.2 removes the debounced auto-POST from _saveAvailability(). "
        "POST now happens only inside _commitAvailUpdate() via the UPDATE "
        "button click."
    )

    # _renderAvailUpdateBtn applies/removes the 'dirty' class based on
    # _availDirty.
    render_btn_match = re.search(
        r"function\s+_renderAvailUpdateBtn\s*\(\s*\)\s*\{([\s\S]+?)\n\}",
        html,
    )
    assert render_btn_match is not None, "missing function _renderAvailUpdateBtn()"
    render_body = render_btn_match.group(1)
    assert "classList.add('dirty')" in render_body, (
        "_renderAvailUpdateBtn() must add the 'dirty' class when _availDirty"
    )
    assert "classList.remove('dirty')" in render_body, (
        "_renderAvailUpdateBtn() must clear the 'dirty' class when clean"
    )

    # CSS for .dirty must define the pulsing accent state — animation
    # name is the shared marker we lock here.
    assert "#avail-update-btn.dirty" in html, (
        "missing CSS rule for #avail-update-btn.dirty (the pulsing accent state)"
    )
    assert "availUpdatePulse" in html, (
        "missing pulsing keyframes (animation 'availUpdatePulse') for the "
        "dirty UPDATE button"
    )


def test_update_button_has_aria_live_region_for_post_click_confirmation():
    """After clicking UPDATE the user gets a 'Plan reflowed — N sessions
    changed' confirmation. For assistive tech this lives in an
    aria-live='polite' region (#avail-update-confirm) that's announced
    when its text changes."""
    html = _read_dashboard()

    # aria-live region exists, has the expected id, and is polite.
    aria_match = re.search(
        r'<span[^>]*id="avail-update-confirm"[^>]*>',
        html,
    )
    assert aria_match is not None, (
        "missing <span id='avail-update-confirm'> — the aria-live region "
        "where v1.3.2 surfaces the 'Plan reflowed — N sessions changed' "
        "confirmation after an UPDATE click"
    )
    aria_open = aria_match.group(0)
    assert 'aria-live="polite"' in aria_open, (
        "the confirmation region must declare aria-live='polite' so screen "
        "readers announce the update result without interrupting"
    )
    # role=status is the canonical pairing for polite live regions.
    assert 'role="status"' in aria_open, (
        "the confirmation region should also carry role='status'"
    )

    # _commitAvailUpdate() writes the canonical confirmation string into
    # this region — lock the wording and the source of N.
    commit_match = re.search(
        r"async\s+function\s+_commitAvailUpdate\s*\(\s*\)\s*\{([\s\S]+?)\n\}",
        html,
    )
    assert commit_match is not None, "missing async function _commitAvailUpdate()"
    commit_body = commit_match.group(1)
    assert "/api/plan/save-availability" in commit_body, (
        "_commitAvailUpdate() must POST to /api/plan/save-availability"
    )
    assert "sessions_modified" in commit_body, (
        "_commitAvailUpdate() must read sessions_modified from the response"
    )
    assert "Plan reflowed" in commit_body, (
        "confirmation text must include 'Plan reflowed' so the aria-live "
        "region announces it"
    )
    assert "avail-update-confirm" in commit_body, (
        "_commitAvailUpdate() must write the confirmation into "
        "#avail-update-confirm"
    )
    # Auto-fade after 4 s (4000 ms).
    assert "4000" in commit_body, (
        "confirmation must auto-fade after 4 seconds (setTimeout 4000)"
    )


if __name__ == "__main__":
    import unittest

    unittest.main()
