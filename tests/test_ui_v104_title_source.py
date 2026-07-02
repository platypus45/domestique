"""v1.0.4 IMPL-DASHBOARD — title-source cascade smoke tests.

Three substring-presence checks against templates/dashboard.html — same
pattern as test_ui_v103.py (no app fixture; the dashboard is a static
template). Verifies the locked v1.0.4 contract from MASTER §3:

  Modal title + calendar cell label cascade =
    1. session.display_name (NEW canonical Layer 3)
    2. session.zwo_name (existing ZWO `<name>` tag)
    3. session.session_type (last-resort planner intent)

Plus the library filter dropdown is updated to the 16-class canonical
list (v1.0.4 §1) and `mixed` is removed (junk drawer routed by zone
dominance in the new classifier).

Bug context: a planner slot of `vo2max 51min` could pick a 82-min Z2
ZWO due to library mis-classification + `<name>` tag drift. The chart
read the picked file's actual class, but the modal title rendered the
plan's intent — three layers disagreed visibly. The cascade fix makes
the picked file's `display_name` the source of truth for the title.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
DASHBOARD_HTML = REPO_ROOT / "templates" / "dashboard.html"


def _read_dashboard() -> str:
    with open(DASHBOARD_HTML, encoding="utf-8") as f:
        return f.read()


class TestUIv104TitleSource(unittest.TestCase):
    """v1.0.4 modal title + calendar cell + library filter contracts."""

    def test_modal_title_is_slot_centric(self):
        """SUPERSEDED v1.0.4 → B1 (2026-06-20): the day-detail modal title is now
        SLOT-centric, not file-centric. A slot whose sampled .zwo was a different
        workout used to read as the wrong session (display_name titled the modal).
        B1 titles from the SLOT — CAL_CONTENT_LABEL[content_class] →
        CAL_SESSION_LABEL[session_type] — plus the slot duration, and shows the
        matched file as ONE secondary line. Locks the new contract so a refactor
        can't silently revert to file-titling.
        """
        html = _read_dashboard()
        self.assertIn("const slotLabel", html)
        self.assertIn("CAL_CONTENT_LABEL[session.content_class]", html)
        self.assertIn("CAL_SESSION_LABEL[session.session_type]", html)
        # v2.2.12 (0ef58e0a) renamed sessionDur→dispDur: the headline shows the
        # matched FILE's duration (deliberate). Slot-centric label unchanged.
        self.assertRegex(
            html,
            r"const\s+heroTitle\s*=\s*`\$\{slotLabel\}\s*\(\$\{dispDur\}min\)`",
            "modal hero title must be `${slotLabel} (${dispDur}min)` (B1 slot-centric)",
        )
        # Matched file is a SECONDARY line now, not the title source.
        self.assertIn("Matched library file:", html)

    def test_calendar_cell_uses_display_name_cascade(self):
        """`calCardTitle(planned)` (used by the weekly calendar cell label
        builders at L7592, L8069 and beyond) must consume `display_name`
        first so the cell label matches the picked file's actual class.
        """
        html = _read_dashboard()
        # Locate the calCardTitle function body.
        m = re.search(
            r"function\s+calCardTitle\s*\(\s*planned\s*\)\s*\{(.*?)\n\}",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m, "calCardTitle(planned) function not found in dashboard.html"
        )
        body = m.group(1)
        # display_name must be referenced BEFORE content_class in the cascade.
        idx_display = body.find("display_name")
        idx_content = body.find("content_class")
        self.assertGreaterEqual(
            idx_display, 0,
            "calCardTitle must read planned.display_name (v1.0.4 cascade)",
        )
        self.assertGreaterEqual(
            idx_content, 0,
            "calCardTitle must still reference content_class as a fallback",
        )
        self.assertLess(
            idx_display, idx_content,
            "calCardTitle must check display_name BEFORE content_class — "
            "MASTER §3 cascade order",
        )
        # zwo_name must also be in the cascade as the second-tier fallback.
        self.assertIn("zwo_name", body)

    def test_library_filter_has_16_classes_no_mixed(self):
        """Library filter dropdown (`#wk-content-class`) must list all 16
        canonical classes from MASTER §1 and must NOT contain `mixed`.
        """
        html = _read_dashboard()
        # Pull out the dropdown block so we don't accidentally match these
        # values elsewhere in the file (planner maps, content-classifier
        # JSON keys, etc.).
        m = re.search(
            r'<select\s+id="wk-content-class"[^>]*>(.*?)</select>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m, '#wk-content-class library filter dropdown not found'
        )
        select_block = m.group(1)
        canonical_16 = (
            "recovery",
            "endurance",
            "endurance_intervals",
            "tempo",
            "tempo_intervals",
            "tempo_ladder",
            "sweet_spot",
            "sweet_spot_ladder",
            "threshold",
            "threshold_ladder",
            "over_under",
            "vo2max",
            "vo2_short",
            "vo2_ladder",
            "anaerobic",
            "neuromuscular",
            "ftp_test",
        )
        for cls in canonical_16:
            self.assertIn(
                f'value="{cls}"',
                select_block,
                f"Library filter must include `value=\"{cls}\"` (v1.0.4 §1)",
            )
        # `mixed` must be dropped entirely — the new classifier routes by
        # zone dominance instead of bucketing into a junk drawer.
        self.assertNotIn(
            'value="mixed"',
            select_block,
            "Library filter must NOT contain `value=\"mixed\"` — v1.0.4 §1 drops it",
        )


if __name__ == "__main__":
    unittest.main()
