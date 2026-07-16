"""3.4.3 — DOMESTIQUE_HOME override (dev-preview sandbox contract).

Env set → ALL user-data module constants resolve under it (spot-checked via
subprocess so import-time constants are exercised for real). Env absent →
byte-identical legacy behavior (~/.domestique). The packaged app never sets
the env, so shipping behavior is untouched.
"""
import os
import subprocess
import sys
from pathlib import Path

_PROBE = (
    "import db, training_planner, power_curve, log_config, app;"
    "print(db._USER_DATA);"
    "print(training_planner.PLAN_DIR);"
    "print(power_curve._profile_dir());"
    "print(log_config.LOG_DIR);"
    "print(app._user_data_dir)"
)


def _run(env_home: str | None) -> list[str]:
    env = os.environ.copy()
    env.pop("DOMESTIQUE_HOME", None)
    if env_home is not None:
        env["DOMESTIQUE_HOME"] = env_home
    out = subprocess.run([sys.executable, "-c", _PROBE],
                         capture_output=True, text=True, env=env,
                         cwd=str(Path(__file__).resolve().parent.parent),
                         timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    return out.stdout.strip().splitlines()


def test_env_absent_resolves_real_home():
    # NOTE: the suite's hermetic conftest sandbox sets HOME per-process; the
    # subprocess inherits that HOME, so "real home" here = the sandbox HOME —
    # which is exactly the legacy Path.home()/".domestique" contract.
    lines = _run(None)
    home = Path(os.environ["HOME"])
    assert all(str(home / ".domestique") in l for l in lines), lines


def test_env_set_redirects_every_constant(tmp_path):
    sandbox = tmp_path / "devhome"
    lines = _run(str(sandbox))
    assert all(str(sandbox) in l for l in lines), lines
    assert not any(".domestique" in l for l in lines), lines
