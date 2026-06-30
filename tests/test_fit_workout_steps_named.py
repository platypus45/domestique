"""Regression: exported FIT workouts must name every step.

Unnamed steps made TrainingPeaks / Vekta report "no workout in this file" —
Garmin's canonical workout files set wkt_step_name on every step, and ours didn't.
This guards both the blocks path and the ZWO-transcode path (what users download).
"""
import glob
import io
import os

import fitparse

import app


def _step_names(data: bytes):
    f = fitparse.FitFile(io.BytesIO(data))
    return [
        {d.name: d.value for d in m}.get("wkt_step_name")
        for m in f.get_messages("workout_step")
    ]


def test_blocks_path_names_every_step():
    names = _step_names(app.build_fit_workout_bytes("threshold", 60, "T", None))
    assert names, "no workout steps emitted"
    assert all(n for n in names), f"unnamed steps: {names}"


def test_zwo_transcode_names_every_step():
    zwo = os.path.basename(glob.glob("workouts/vo2*.zwo")[0])
    names = _step_names(app.build_fit_workout_bytes("vo2max", 45, "w", zwo))
    assert names, "no workout steps emitted"
    assert all(n for n in names), f"unnamed steps: {names}"
