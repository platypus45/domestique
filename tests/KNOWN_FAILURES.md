# The gate: no new failures against `clean-main`

`clean-main` is **not green**, so the gate for this branch is *no NEW failing
test IDs versus main's set* — the set, never the count.

## Run it with `--timeout=180`, not 45

The original gate used `--timeout=45`, and six `test_357_block_evaluation.py`
entries in the baseline below were **not failures at all**. They were
pytest-timeout kills: the file's interval-grading cases take ~11s each and
under `-n 8` contention a load-dependent subset crosses a 45s ceiling. Measured:

```
tests/test_357_block_evaluation.py  -n 8 --timeout=45    13-14 failed, 38-39 passed  (~92s)
tests/test_357_block_evaluation.py  -n 8 --timeout=180   52 passed, 0 failed        (205s)
```

*Which* cases trip the ceiling changes run to run, and the same command on a
`git archive` of clean-main fails the same way — so this was never a property of
this branch, and 8 of the ~14 that time out were not even in the recorded
baseline. A gate that lists a load-dependent set as "known failures" can mask a
real regression in any of those 52 tests and invent one on a busier machine.

Found by an independent review of this branch, not by the author.

## The baseline

```
clean-main            15 failed, 3,3xx passed   (-n 8, --timeout=180)
refactor/backend-...  15 failed
diff of failing IDs   empty, both directions
```

| File | Failures | Why, as far as it goes |
|---|---|---|
| `test_tls_trust.py` | 4 | OpenSSL root/interceptor handling — environmental |
| `test_download_pywebview_bridge.py` | 4 | `pywebview` is deliberately absent from the headless venv |

The seven FIT export failures once listed here (`test_fit_hr_mode.py` 5,
`test_ftp_test_freeride.py` 2) were a real bug, not the environment: step
durations were pre-multiplied by 1000 and fit_tool applied the field's 1000x
scale again, so a 60-minute step decoded as 1,000 hours. Fixed 2026-09-14 by
setting the seconds sub-field (`duration_time`) instead.

A likely contributor to the first and last: a fresh worktree checks out
**upstream's malformed `ftp_test_*.zwo` files**, the ones carrying `pace="warmup"`
and `pace="ramp_test"` as free text (`upstream-findings.md` §1). The live
deployment repairs them with `cs-repair-workouts`; `clean-main` does not, and
neither does a worktree. Not investigated further — they fail identically on
main, so they are not this branch's problem.

## Running it

```
PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
uv run --with pytest --with pytest-xdist --with pytest-timeout \
       --with fitparse --with Pillow --python .venv/bin/python \
       -m pytest -q -p no:cacheprovider -n 8 --timeout=180 --tb=no -rf
```

`fitparse` and `Pillow` are stripped from the headless venv by the stack's
updater, so three files fail to *collect* without `--with`.

The run dirties `src/workouts/.library_index.json`; `git checkout --` it
afterwards.

To re-diff against main:

```
git worktree add --detach ~/Documents/domestique-main origin/clean-main
ln -sfn <the shared venv> ~/Documents/domestique-main/.venv
# run the command above in each tree, keeping only "^FAILED " lines, and diff
```

**Per-commit gate** — seconds, not minutes:

1. `tests/characterize_planner.py` — 57 planner fingerprints, must be unchanged
2. the test files covering whatever the commit touched
3. `import app` under a temp `DOMESTIQUE_HOME`, asserting the re-exports that
   the suite reaches through `app.X`

The full suite runs before the branch is handed back, and is diffed against
main rather than read as a pass/fail.
