"""v1.2.1 IMPL-PANEL — dashboard banister-validation panel string-presence tests.

Locks the DOM + JS contracts for the out-of-sample Banister-validation
panel inserted under the τ-fits panel inside the CTL card. Tests are
plain string-presence smoke checks against templates/dashboard.html —
they do NOT spin up Flask. The endpoint behaviour is locked separately
in tests/test_oos_validation.py (v1.2.0).

Coverage (per /tmp/MASTER_DECISIONS_v121_PATCH.md):
  1. Panel present — <details class=...> + id + JS function names.
  2. Panel ordering — τ-fits panel comes BEFORE banister-validation panel.
  3. All 16 locked field bindings appear verbatim (G12 patch).
  4. <script> tag balance + new function names present (G13 patch).
  5. Accessibility — aria-busy + aria-label literals present (G5 patch).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_banister_panel_present():
    html = (REPO_ROOT / "templates/dashboard.html").read_text()
    assert '<details class="banister-validation"' in html
    assert 'id="banister-oos-panel"' in html
    assert 'loadBanisterValidationPanel' in html
    assert 'refreshBanisterValidation' in html
    assert 'renderBanisterValidation' in html


def test_panel_ordering():
    html = (REPO_ROOT / "templates/dashboard.html").read_text()
    assert html.index('"tau-fit-results"') < html.index('"banister-validation"')


def test_all_16_locked_fields_present():
    html = (REPO_ROOT / "templates/dashboard.html").read_text()
    for f in (
        "ftp_mae_w", "ftp_mae_pct", "ftp_mae_pct_ci_low", "ftp_mae_pct_ci_high",
        "ctl_mae_tss", "ctl_mae_tss_ci_low", "ctl_mae_tss_ci_high",
        "hellard_2006_baseline_pct", "comparison",
        "cp_fitness_mae_pct", "wprime_fitness_mae_pct", "pmax_fitness_mae_pct",
        "tau_fits_used", "n_markers_in_holdout", "holdout_weeks", "horizon_weeks",
    ):
        assert f in html, f"locked field binding missing: {f}"


def test_no_unbalanced_script_tags():
    html = (REPO_ROOT / "templates/dashboard.html").read_text()
    assert html.count("<script") == html.count("</script>"), "unbalanced <script> tags"
    assert "function loadBanisterValidationPanel" in html
    assert "function refreshBanisterValidation" in html
    assert "function renderBanisterValidation" in html


def test_a11y_attributes_present():
    html = (REPO_ROOT / "templates/dashboard.html").read_text()
    assert "aria-busy" in html
    assert 'aria-label="Re-run holdout validation"' in html
