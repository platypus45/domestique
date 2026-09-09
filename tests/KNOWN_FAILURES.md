# Pre-existing test failures at the branch point

Measured on `refactor/backend-architecture` at the baseline commit, before any
extraction, so that later steps can be judged against reality rather than
against "green".

```
3,387 tests · 8m42s with -n 8 · 21 failed, 3,351 passed, 9 skipped,
15 xfailed, 12 xpassed, 28 subtests passed
```

**The gate for this branch is therefore "no NEW failures", not "all green".**

| File | Failures | Looks like |
|---|---|---|
| `test_357_block_evaluation.py` | 10 | interval-workout grading over the library; parameterised (`stopped early`, `all harder`, `warm-up ramp laps`, `all longer`, `forgotten lap`, `long recoveries`) |
| `test_fit_hr_mode.py` | 5 | FIT export in HR mode |
| `test_tls_trust.py` | 4 | OpenSSL root/interceptor handling — environmental |
| `test_ftp_test_freeride.py` | 2 | FIT export, open-target blocks |

Confirmed pre-existing rather than assumed:

- `test_tls_trust.py`, `test_fit_hr_mode.py` and `test_ftp_test_freeride.py`
  were run at the base commit with the extraction reverted: **11 failed**, the
  same 11 they contribute to the full run.
- `test_357_block_evaluation.py` imports only `glob`, `json`, `pytest` and
  `structure_fidelity`. It never imports `app`, and greps zero times for any
  name the extraction moved, so it cannot be affected by it.

## Running the suite

The full run is slow enough that it is not a per-commit gate:

```
uv run --with pytest --with pytest-xdist --with pytest-timeout \
       --with fitparse --with Pillow --python .venv/bin/python \
       -m pytest -q -p no:cacheprovider -n 8 --timeout=45
```

`fitparse` and `Pillow` are deliberately absent from the headless venv (the
stack's updater strips GUI dependencies), so three files fail to collect
without `--with`. `-n 8` takes it from over 15 minutes to under 9.

**Per-commit gate** is the fast triple, which runs in seconds:

1. `tests/characterize_planner.py` — 57 planner fingerprints, must be unchanged
2. the test files covering whatever the commit touched
3. `python -c "import app"` under a temp `DOMESTIQUE_HOME`, checking the
   re-exports the suite reaches through `app.X`

The full suite runs before the branch is handed back, not on every commit.
