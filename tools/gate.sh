#!/usr/bin/env bash
# Full-suite gate for the Domestique overhaul, run identically at every step.
#
#   gate.sh <worktree> <label> [baseline-label]
#
# 1. full pytest, -n 5, failures AND errors summarised (-rfE)
# 2. every FAILED/ERROR id not in tests/known-failures-clean-main.txt is
#    re-run ALONE, serially: "real" if it still fails, "passes-alone" if not
#    (load-dependent timeouts, or pollution from another test under xdist)
# 3. with a baseline label: NEW = real failures absent from the baseline's
#    real set, and passes-alone ids absent from the baseline are listed too
set -u
WT=${1:?worktree}; LABEL=${2:?label}; BASE=${3:-}
S=${GATE_OUT:-/tmp/domestique-gate}
mkdir -p "$S"
OUT=$S/$LABEL
cd "$WT" || exit 2
export PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
# fitparse and Pillow: without them three test modules fail to import (the
# gates review, GATE-8). Pillow is a declared requirement the prod venv lacks.
PYT=(uv run --quiet --with pytest --with pytest-timeout --with pytest-xdist
     --with fitparse --with Pillow
     --python .venv/bin/python -m pytest -p no:cacheprovider)

date > "$OUT.full.txt"
timeout 3300 "${PYT[@]}" -n 5 -q --timeout=900 --tb=no -rfE tests/ >> "$OUT.full.txt" 2>&1
grep -E '^(FAILED|ERROR) ' "$OUT.full.txt" | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' \
  | sort -u > "$OUT.bad.txt"
comm -23 "$OUT.bad.txt" <(sort tests/known-failures-clean-main.txt) > "$OUT.unknown.txt"

: > "$OUT.real.txt"; : > "$OUT.alone.txt"
while read -r t; do
  [ -z "$t" ] && continue
  if timeout 900 "${PYT[@]}" -q --timeout=600 --tb=short "$t" > "$OUT.rerun.$(echo "$t" | md5sum | cut -c1-8).log" 2>&1
  then echo "$t" >> "$OUT.alone.txt"; else echo "$t" >> "$OUT.real.txt"; fi
done < "$OUT.unknown.txt"
git checkout -- src/workouts/.library_index.json src/workouts/.workout_facts.json 2>/dev/null

echo "== $LABEL: $(grep -E '(passed|failed).* in [0-9.]+s' "$OUT.full.txt" | tail -1)"
echo "not-known: $(wc -l < "$OUT.unknown.txt")   real: $(wc -l < "$OUT.real.txt")   passes-alone: $(wc -l < "$OUT.alone.txt")"
echo "-- real:";         sed 's/^/   /' "$OUT.real.txt"
echo "-- passes-alone:"; sed 's/^/   /' "$OUT.alone.txt"
if [ -n "$BASE" ]; then
  echo "-- NEW real vs $BASE:";         comm -23 "$OUT.real.txt"  <(sort "$S/$BASE.real.txt")  | sed 's/^/   /'
  echo "-- new passes-alone vs $BASE:"; comm -23 "$OUT.alone.txt" <(sort "$S/$BASE.alone.txt") | sed 's/^/   /'
fi
