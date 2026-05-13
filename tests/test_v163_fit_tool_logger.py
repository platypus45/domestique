"""v1.6.3 — fit_tool's per-record WARNINGs are suppressed.

The third-party ``fit_tool`` library logs at WARNING for every
non-standard FIT field it encounters. Garmin devices stamp ~3,000
records per hour-long ride and each produces 1-2 lines, so a first-boot
ICU sync over ~10 rides floods the log to >13,000 lines / 5 MB inside
70 s. Those warnings carry no actionable signal -- any genuine parse
failure already raises an exception. ``log_config.setup_logging`` pins
the ``fit_tool`` logger to ERROR.
"""
from __future__ import annotations

import logging

import log_config


def test_fit_tool_logger_at_least_error_after_setup():
    log_config.setup_logging()
    lg = logging.getLogger("fit_tool")
    assert lg.level >= logging.ERROR, (
        f"fit_tool level is {lg.level} ({logging.getLevelName(lg.level)}); "
        "expected >= ERROR (40)"
    )


def test_fit_tool_warnings_are_dropped():
    log_config.setup_logging()
    lg = logging.getLogger("fit_tool")

    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    h = _CapturingHandler()
    h.setLevel(logging.DEBUG)
    try:
        lg.addHandler(h)
        lg.warning("Field id: 108 is not defined for message record:20.")
        lg.warning("Record 1234, size mismatch.")
        lg.error("genuine parse fault")
    finally:
        lg.removeHandler(h)

    assert "genuine parse fault" in captured
    # Both warnings must be filtered out by the level pin.
    assert not any("Field id: 108" in m for m in captured)
    assert not any("Record 1234" in m for m in captured)
