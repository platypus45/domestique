"""v1.6.1 — frontend instrumentation test.

Asserts every new ``E_FRONTEND_*`` code added in v1.6.1 appears at least
once as a string literal in ``templates/dashboard.html``. This is a
build-time consistency check that catches the case where a code was
registered in ``error_codes.py`` but never wired into a JS site.

Regex-grep is intentionally simple: a hit anywhere in the file (including
comments) counts. The frontend tests are not unit tests of behaviour —
they verify that the wiring layer exists. Behavioural tests for the
backend logging path live in ``test_v161_planner_logging.py`` and
``test_v161_homepage_endpoint_logging.py``.
"""
from __future__ import annotations

import unittest
from pathlib import Path


_DASHBOARD = (
    Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
)

# v1.6.1 codes added for homepage panel instrumentation. Each must appear
# at least once in dashboard.html.
_V161_FRONTEND_CODES = (
    "E_FRONTEND_HOMEPAGE_INIT",
    "E_FRONTEND_FITNESS_FETCH",
    "E_FRONTEND_FITNESS_PARSE",
    "E_FRONTEND_FITNESS_RENDER",
    "E_FRONTEND_ACTIVITIES_FETCH",
    "E_FRONTEND_ACTIVITIES_PARSE",
    "E_FRONTEND_ACTIVITIES_RENDER",
    "E_FRONTEND_READINESS_FETCH",
    "E_FRONTEND_READINESS_PARSE",
    "E_FRONTEND_READINESS_RENDER",
    "E_FRONTEND_TODAY_SESSION_FETCH",
    "E_FRONTEND_TODAY_SESSION_PARSE",
    "E_FRONTEND_TODAY_SESSION_RENDER",
    "E_FRONTEND_EFTP_FETCH",
    "E_FRONTEND_EFTP_PARSE",
    "E_FRONTEND_EFTP_RENDER",
    "E_FRONTEND_BODY_PERF_FETCH",
    "E_FRONTEND_BODY_PERF_PARSE",
    "E_FRONTEND_BODY_PERF_RENDER",
    "E_FRONTEND_ENERGY_SYS_RENDER",
)


class FrontendCodeWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_text = _DASHBOARD.read_text(encoding="utf-8")

    def test_dashboard_template_exists(self):
        self.assertTrue(_DASHBOARD.is_file(),
                        f"templates/dashboard.html missing at {_DASHBOARD}")

    def test_each_v161_frontend_code_referenced(self):
        missing = [c for c in _V161_FRONTEND_CODES
                   if c not in self.template_text]
        self.assertEqual(missing, [],
                         f"v1.6.1 frontend codes missing from "
                         f"templates/dashboard.html: {missing}")

    def test_diag_helper_invoked_for_v161_codes(self):
        """Each v1.6.1 code should appear inside a ``_diagFrontendError(``
        call (string-literal check). Catches the case where a code is
        named in a comment but never actually emitted.
        """
        # Trim down to lines that mention _diagFrontendError so the
        # substring check below has a tight scope.
        emit_lines = [
            line for line in self.template_text.splitlines()
            if "_diagFrontendError" in line
        ]
        emit_blob = "\n".join(emit_lines)
        not_emitted = [c for c in _V161_FRONTEND_CODES if c not in emit_blob]
        self.assertEqual(not_emitted, [],
                         f"codes never appear inside a _diagFrontendError "
                         f"call: {not_emitted}")


if __name__ == "__main__":
    unittest.main()
