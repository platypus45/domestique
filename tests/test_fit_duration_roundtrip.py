"""v3.11.7 -- a FIT workout carries the workout's real step lengths.

Every build since fit-tool 0.9.16 (2026-08-05) exported each step 1000x too
long: the code multiplied seconds by 1000 itself and 0.9.16's setter scales
again. The dev interpreter had 0.9.15 and the builds 0.9.16 (the requirement
was a range), and the only duration test read the file back through fit_tool,
so nothing saw it. macOS builds were affected from v3.11.2, Windows and Linux
longer. Found by taladjidi (platypus45/domestique#14).

These tests decode with fitparse -- an independent decoder -- and pin the
dependency, so the spelling in app.py and the installed library cannot drift
apart again without a red test.
"""
from __future__ import annotations

import importlib.metadata
import io
import re
import sys
from pathlib import Path

import fitparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import app  # noqa: E402

ZWO = "endurance_steady_55pct_60min.zwo"


def _decoded_total_s(data: bytes) -> float:
    return sum((m.get_value("duration_time") or 0)
               for m in fitparse.FitFile(io.BytesIO(data)).get_messages("workout_step"))


def _zwo_total_s(name: str) -> int:
    src = (REPO / "src" / "workouts" / name).read_text()
    return sum(int(x) for x in re.findall(r'Duration="(\d+)"', src))


def test_a_library_workout_exports_with_its_own_length():
    data = app.build_fit_workout_bytes("z2", 60, "roundtrip", ZWO)
    assert _zwo_total_s(ZWO) == 3600
    assert round(_decoded_total_s(data)) == 3600


def test_the_generic_builder_exports_the_requested_minutes():
    """No library file: the block builder (the other duration site in app.py)."""
    data = app.build_fit_workout_bytes("z2", 45, "roundtrip-generic")
    total = _decoded_total_s(data)
    assert abs(total - 45 * 60) <= 60, f"generic 45-minute workout decodes as {total:.0f} s"


def test_no_step_is_longer_than_the_workout():
    data = app.build_fit_workout_bytes("vo2max", 60, "roundtrip-steps")
    steps = [m.get_value("duration_time") or 0
             for m in fitparse.FitFile(io.BytesIO(data)).get_messages("workout_step")]
    assert steps and max(steps) <= 60 * 60, f"longest step {max(steps):.0f} s in a 60-minute workout"


def test_fit_tool_is_pinned_exactly_and_installed_at_the_pin():
    req = (REPO / "requirements.txt").read_text()
    m = re.search(r"^fit-tool==([0-9.]+)\b", req, re.M)
    assert m, "requirements.txt must pin fit-tool with ==; a range let the builds drift to a version the code was not written for"
    installed = importlib.metadata.version("fit-tool")
    assert installed == m.group(1), (
        f"fit-tool {installed} is installed but requirements.txt pins {m.group(1)}: "
        f"run `pip install -r requirements.txt`; the two versions scale step durations differently")
