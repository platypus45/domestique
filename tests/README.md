# tests/

Pytest suite for Domestique. Covers planner, calendar, ride storage, FIT
classification, route picking, training-load math, wellness sync,
content classification, and HTTP endpoints.

## Run

From repo root:

```sh
pytest -q
```

Pytest auto-discovers `tests/test_*.py`. `pytest.ini` ships
`addopts = -m "not hardware"` so hardware-gated tests (BLE/ANT
trainer-control, deprecated since v4.0.0-alpha) stay skipped by
default. Re-enable explicitly with `pytest -m hardware`.

There is no `conftest.py` — each test file does its own
`sys.path.insert(0, REPO_ROOT)` if it needs to import a root-level
module by bare name. Tests that read source files via
`Path(__file__)` (e.g. `test_planner_fixes.py`,
`test_route_shape_primitives.py`, `test_training_live.py`,
`test_content_classifier.py`) use `Path(__file__).parent.parent`
to reach repo root.

## Layout

This folder mixes endpoint-level integration tests (FastAPI
TestClient against `app.py`) and unit tests (pure functions in
`training_planner.py`, `analytics.py`, `route_archetypes.py`,
`zones.py`, etc.). They aren't physically split — pytest discovers
both flavours together.

## Baseline

Target: 783 passed. Three pre-existing flakes in the wellness /
training-load area pass on a clean checkout but can race when the
local SQLite DB has stale state from a prior run; rerun if one
fails. They are tracked separately and not introduced by the v1.0
cleanup.

## Relocation history

Most `test_*.py` files were relocated from repo root into `tests/`
as part of the v1.0 layout cleanup. See git log for that commit.
