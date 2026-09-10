# The gate: no new failures against `clean-main`

`clean-main` is **not green**. Measured by running the full suite on a clean
worktree of `origin/clean-main` and on this branch, capturing the failing test
IDs from each and diffing them:

```
clean-main            21 failed, 3,350 passed   (8m41s, -n 8)
refactor/backend-...  21 failed, 3,351 passed
diff of failing IDs   empty, both directions — byte-identical sets
```

So the gate for this branch is **no NEW failures versus main's set**, and the
comparison is the set of test IDs, not the count. Counts alone mislead: an
earlier read of two run tails showed 22 vs 21 and suggested main was worse,
which the ID diff disproved. At least one of these tests is flaky.

## The 21, by file

| File | Failures | Why, as far as it goes |
|---|---|---|
| `test_357_block_evaluation.py` | 6 | interval grading across the workout library |
| `test_fit_hr_mode.py` | 5 | FIT export in HR mode |
| `test_tls_trust.py` | 4 | OpenSSL root/interceptor handling — environmental |
| `test_download_pywebview_bridge.py` | 4 | `pywebview` is deliberately absent from the headless venv |
| `test_ftp_test_freeride.py` | 2 | FIT export, open-target blocks |

A likely contributor to the first, second and fifth: a fresh worktree checks out
**upstream's malformed `ftp_test_*.zwo` files**, the ones carrying `pace="warmup"`
and `pace="ramp_test"` as free text. That is `upstream-findings.md` §1. The live
deployment repairs them with `cs-repair-workouts`; `clean-main` does not, and
neither does a worktree. Not investigated further — they fail identically on
main, so they are not this branch's problem.

## Running it

```
uv run --with pytest --with pytest-xdist --with pytest-timeout \
       --with fitparse --with Pillow --python .venv/bin/python \
       -m pytest -q -p no:cacheprovider -n 8 --timeout=45 --tb=no -rf
```

`fitparse` and `Pillow` are stripped from the headless venv by the stack's
updater, so three files fail to *collect* without `--with`. `-n 8` takes the run
from over 15 minutes to under 9.

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
