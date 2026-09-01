"""v1.0.3 fix-forward(workout-detail UX) — JS-side filename derivation.

User report: clicking ``Download ZWO`` gave ``tempo_4x150s_85pct_63min.zwo``;
clicking ``Download FIT`` for the same session gave ``Tuesday_TEMPO.fit``.
That mismatch confused users who expected to download "the same workout in
a different format".

The fix in ``templates/dashboard.html`` constructs ``fitName`` from the
matched ZWO file's basename (when available) so both buttons produce names
sharing the same prefix. This test pins the substring so the fix can't
regress without breaking the test.

There's no headless JS engine in the test harness, so this is a textual
assertion on the rendered template — the next-best thing to actually
running the code in pywebview's WebKit.
"""
from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "src" / "templates" / "dashboard.html"


class TestFilenameConsistency(unittest.TestCase):
    def test_fit_button_derives_from_zwo_file(self):
        """The dashboard must build the FIT name from ``session.zwo_file``
        when the session resolved to a library file, so the FIT and the ZWO
        share a basename. The substring below is the load-bearing literal
        introduced by the v1.0.3 fix-forward; if it disappears the bug is
        back."""
        text = DASHBOARD.read_text(encoding="utf-8")
        self.assertIn(
            "session.zwo_file.replace(/\\.zwo$/i, '')",
            text,
            "templates/dashboard.html should derive fitName from session.zwo_file",
        )

    def test_downloadFIT_signature_takes_zwo_file(self):
        """``downloadFIT`` must accept the optional 4th arg so the planner
        modal can plumb the matched ZWO basename through to the server."""
        text = DASHBOARD.read_text(encoding="utf-8")
        self.assertIn(
            "async function downloadFIT(type, durationMin, name, zwoFile",
            text,
            "downloadFIT should accept an optional zwoFile parameter",
        )


if __name__ == "__main__":
    unittest.main()
