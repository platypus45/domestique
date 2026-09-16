"""Filesystem locations the whole backend resolves through.

These sat in app.py's prelude and were reached from ten sections, which made
the prelude a dependency of every would-be feature module. Nothing here is new
behaviour; the definitions are byte-for-byte what app.py had.

Two deliberate choices:

`_plan_dir()` imports training_planner inside the function, not at module
scope. The live plan directory is `training_planner.PLAN_DIR`, which
profile_manager reassigns on a profile switch, so it has to be read late --
and importing the 14k-line planner at module scope would make this module the
opposite of substrate. `_rides_fit_dir()` defers to ride_storage for the same
reason. Function-local imports are the established cycle-avoidance convention
in this codebase and are load-bearing, not untidiness.

The active profile's workout and GPX directories are NOT here. They are
rebound on every profile switch, so they are read through app's
active_workout_dir() / active_gpx_dir() accessors --
tests/test_profile_path_rebinding.py enforces that nothing binds them by value.
"""
from pathlib import Path

from user_home import domestique_home

_user_data_dir = domestique_home()  # 3.4.3: DOMESTIQUE_HOME-aware
DATA_DIR = _user_data_dir

COURSE_DIR = Path(__file__).parent / "courses"
ROUTE_DATA = Path(__file__).parent / "routes.json"
ROUTE_PROFILES_INDEX = Path(__file__).parent / "profiles_indexed.json"
ROUTE_PROFILES_DIR = Path(__file__).parent / "profiles"

_DEFAULT_PLAN_DIR = _user_data_dir / "plans"


def _plan_dir() -> Path:
    """Dynamic plan dir: uses profile-specific path after profile switch."""
    import training_planner as tp
    d = getattr(tp, 'PLAN_DIR', _DEFAULT_PLAN_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, *parts: str) -> Path | None:
    """Resolve path and verify it's inside the base directory (prevent traversal + symlink escape)."""
    try:
        path = base.joinpath(*parts).resolve()
        base_resolved = base.resolve()
        # Use is_relative_to (Python 3.9+) for robust check
        if hasattr(path, 'is_relative_to'):
            if not path.is_relative_to(base_resolved):
                return None
        else:
            # Fallback: string prefix with trailing separator
            if not (str(path) + "/").startswith(str(base_resolved) + "/"):
                return None
        return path
    except (ValueError, OSError):
        return None


def _rides_fit_dir() -> Path:
    """Directory for raw FIT imports — v3.0.0 AC2a: PER-PROFILE, delegated to
    ride_storage._fit_rides_dir() so app.py and ride_storage.load_all_rides
    can never disagree about where FITs live (the old global
    ~/.domestique/rides made one profile's imports visible to all, and after
    the per-profile migration an app-side global would make imports vanish
    from load_all_rides entirely)."""
    import ride_storage as _rs
    return _rs._fit_rides_dir()
