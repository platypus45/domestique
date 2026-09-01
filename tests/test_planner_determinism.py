"""v2.0.3 F4 (T-DET) — generate_plan is byte-deterministic on a COLD module.

Regression guard for F4: tp:4310 read the module-global
``_CONTENT_CLASSIFICATION_CACHE`` directly instead of the lazy loader
``_load_content_classifications()``. On a cold first call the global is None →
cache={} → ``_is_interval_shaped`` returns False for every slot → the
interval-floor swap post-pass fires spuriously, burning extra RNG (shuffle +
random) and desyncing the per-week stream. The result: a cold first
``generate_plan`` call diverged from every subsequent (warm) call.

This test runs three back-to-back ``generate_plan`` calls inside ONE fresh
Python subprocess (so the very first call sees the cold global) and asserts the
emitted plans are byte-identical. A subprocess is mandatory — running in-process
would warm the global via the test collection / other tests and hide the bug.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent / "src"


_DRIVER = textwrap.dedent(
    """
    import json, sys
    from datetime import date, timedelta
    from dataclasses import asdict
    sys.path.insert(0, {repo!r})
    import training_planner as tp

    def _signature():
        goal = tp.Goal(
            goal_type="event",
            target_date=date.today() + timedelta(weeks=24),
            event_type="sportive",
            event_km=200,
            hours_per_week=8.0,
            max_weekday_hours=2.0,
            max_weekend_hours=4.0,
            plan_weeks=24,
        )
        # Pin CTL/volume (W8 values) — byte-determinism must not depend on the
        # live ICU wellness fetch or the machine-local ride archive. The
        # subprocess also inherits DOMESTIQUE_NO_NET=1 from conftest, so even
        # an accidental self-fetch can never reach the live API.
        phases, weeks = tp.generate_plan(
            goal, seed_salt=4242, current_ctl=50.0, recent_weekly_tss=650.0,
        )
        # The load-bearing determinism signal is the ordered (week, day) ->
        # (session_type, zwo_file, tss) sequence. Serialize it stably.
        rows = []
        for w in weeks:
            for s in w.sessions:
                rows.append([
                    w.week_num, w.phase,
                    s.day.isoformat() if hasattr(s.day, "isoformat") else str(s.day),
                    s.session_type, s.zwo_file or "", round(float(s.tss_estimate or 0), 1),
                ])
        return json.dumps(rows, sort_keys=True)

    a = _signature()  # COLD first call — global cache is None here
    b = _signature()  # warm
    c = _signature()  # warm
    print("EQ" if (a == b == c) else "NEQ")
    """
).format(repo=str(_REPO))


def test_generate_plan_cold_call_is_byte_deterministic():
    """call0 == call1 == call2 in a single cold fresh subprocess."""
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert proc.stdout.strip().splitlines()[-1] == "EQ", (
        "cold generate_plan diverged from warm calls (F4 regression):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_two_cold_subprocesses_agree():
    """Two independent cold subprocesses must produce the same first-call plan
    — i.e. determinism is not just intra-process self-consistency but stable
    across cold starts (guards the regen-drift symptom #83)."""
    def _first_call_sig() -> str:
        driver = _DRIVER.replace(
            'print("EQ" if (a == b == c) else "NEQ")',
            "print(a)",
        )
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip().splitlines()[-1]

    assert _first_call_sig() == _first_call_sig()
