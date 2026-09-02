"""FTP-tests F8 (v3.11.3) — the two-step "Schedule FTP test" modal and the
"selected" card following the MATCHED file.

Text-scan pins (same style as the other dashboard scans): the two panes exist,
the chooser accepts an onChoose override (so the scheduler reuses the cards),
choosing a card in step 2 IS the schedule action, and an auto-matched slot
derives its selected card from the file family instead of defaulting to
20-minute (the "20-minute · selected above a matched Ramp" bug).
"""
from __future__ import annotations

from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "src" / "templates"
        / "dashboard.html").read_text(encoding="utf-8")


def test_two_step_panes_present():
    assert 'id="sched-ftp-step1"' in HTML
    assert 'id="sched-ftp-step2"' in HTML
    assert "function _schedFtpCalendarHtml" in HTML
    assert "_schedFtpStep(2)" in HTML          # Continue → step 2
    assert "_schedFtpStep(1)" in HTML          # change → back to step 1


def test_chooser_reused_with_schedule_action():
    assert "function ftpTestChooserHtml(sessionIdx, day, currentType, opts)" in HTML
    assert "onChoose: tt => `scheduleFtpTestSubmit('${tt}')`" in HTML
    assert "disabled: true, onChoose: () => ''" in HTML   # step-1 greyed preview
    # The old single-form scheduler (date input + protocol select) is gone.
    assert 'id="sched-ftp-proto"' not in HTML


def test_selected_card_follows_matched_file():
    assert "function _ftpTypeFromFile" in HTML
    assert "session.ftp_test_type || _ftpTypeFromFile(session.zwo_file) || 'coggan_20min'" in HTML
    for tok, fam in (("ftp_test_ramp", "ramp"), ("ftp_test_60min", "sixty_min"),
                     ("ftp_test_coggan", "coggan_20min")):
        assert f"f.includes('{tok}')) return '{fam}'" in HTML


def test_day_modal_default_behaviour_unchanged():
    # Default onChoose still routes to chooseFtpTest (the day modal path).
    assert "const onChoose = opts.onChoose || (tt => `chooseFtpTest(${sessionIdx}, '${escJs(day)}', '${tt}')`);" in HTML
