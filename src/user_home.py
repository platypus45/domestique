"""Single resolver for the Domestique user-data home (~/.domestique).

3.4.3 (chip task_23a70236): the dev preview server used to run against the
REAL ~/.domestique — every live plan-tab browse during development mutated
the owner's actual training data (plan rewrites via the open sequence,
sync markers, readiness flags; it polluted two diagnoses on 2026-07-16).

`DOMESTIQUE_HOME`, when set, points ALL user-data resolution at that
directory instead. The packaged app never sets it → env absent = exactly
the old behavior (Path.home()/".domestique"). scripts/dev_preview.sh sets
it to a scratch copy seeded from the real profile.

Import-order contract: this module has NO project imports (stdlib only) so
every module constant (db._USER_DATA, tp.PLAN_DIR, …) can resolve through
it at import time.
"""
from __future__ import annotations

import os
from pathlib import Path


def domestique_home() -> Path:
    """The user-data root: $DOMESTIQUE_HOME if set, else ~/.domestique."""
    env = os.environ.get("DOMESTIQUE_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".domestique"
