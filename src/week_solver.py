"""Choose a week's workouts by solving the assignment, not by nudging it.

THE PROBLEM, STATED. Each training slot in a week takes exactly one workout
from a shortlist (or rest). Every candidate contributes a known number of
minutes to each of four intensity bands. Choose one per slot so the week's band
totals land as close as possible to the prescribed budget, subject to the rules
that make a week trainable: a cap on hard sessions, 48 hours between them,
per-day availability, and the safety floors and ceilings on the distribution.

That is a multiple-choice multidimensional assignment problem with a goal
objective -- textbook, and a solver does it exactly in milliseconds. The
previous approach was greedy: build a week, then apply whichever single swap
looked best, then re-measure. Greedy chases local residuals, because a session
contributes to several bands at once and closing one gap opens another; the
measured symptom was that pushing the scoring harder just moved the breach to a
different band.

FORMULATION (scipy.optimize.milp, backed by HiGHS -- scipy is already a
declared dependency, so this adds nothing to install).

  variables   x[i,j] in {0,1}   slot i takes candidate j
              u[b], o[b] >= 0   under- and over-shoot in band b

  minimise    sum_b (u[b] + o[b])  +  lambda * (uT + oT)

              in SHARE space: the deviation measured is
                  delivered[b] - share[b] * total
              where total is itself a linear expression in x. Both are linear,
              so this stays a MILP.

              Measured, weighting the absolute minutes by 1/target[b] does the
              wrong thing: a Z5+ minute then costs nine times a Z1 minute, so
              the solver nails the small bands and lets volume drift, and the
              SHARES come out skewed hard -- the easy-share gap went from -2.6
              to -9.8 points. What the plan is judged on is the distribution,
              so that is what the objective has to be written in. The separate
              (uT, oT) term pins total volume, which is the load half.

  subject to  sum_j x[i,j] = 1                     one workout per slot
              sum_ij a[i,j,b] x[i,j] - o[b] + u[b] = target[b]
              sum_ij hard[i,j] x[i,j] <= hit_max    recovery, not time
              hard[i] + hard[i+1] <= 1              48 h between hard days
              sum_ij z1[i,j] x[i,j] >= floor * total easy-share floor
              sum_ij z3[i,j] x[i,j] <= ceil * total  intensity ceiling

The last two are the point of doing this properly: the safety rails become
CONSTRAINTS the solution must satisfy, instead of tests that fail afterwards.
They are RATIOS, so a zero-volume week satisfies them vacuously -- and that is
the right answer rather than a hole in the model. Asked for a distribution no
available session can produce, it prescribes rest instead of handing back a
week that breaks the floor. The objective is what stops it resting a week that
has real work owed: an empty week is maximally far from a non-zero target.
Availability is handled upstream -- a candidate that does not fit its day's cap
is never offered.

Returns None when the model is infeasible or scipy is unavailable, and the
caller keeps whatever it already had. A solver that cannot solve must not be
allowed to produce a worse week than the heuristic it replaced.
"""
from __future__ import annotations

from dataclasses import dataclass

BANDS = ("z1z2", "z3", "z4", "z5plus")

# Below this many minutes a band's target is noise rather than a goal, and
# normalising by it would make a rounding error look like a catastrophe.
_BAND_FLOOR_MIN = 10.0

# Cost of a minute of total-volume error, against a minute of misplaced
# intensity. Below 1 because a week that trains the right SHAPE at slightly the
# wrong volume is a better week than the reverse.
_VOLUME_WEIGHT = 0.35


@dataclass(frozen=True)
class Candidate:
    """One option for one slot: what it delivers, and whether it is a hard day."""
    key: object                       # opaque; handed back to the caller
    bands: dict                       # minutes per band, already day-cap clamped
    is_hard: bool = False

    def band(self, b: str) -> float:
        return float(self.bands.get(b, 0.0) or 0.0)

    @property
    def minutes(self) -> float:
        return sum(self.band(b) for b in BANDS)


@dataclass(frozen=True)
class WeekProblem:
    slots: list                       # list[list[Candidate]], one list per slot
    target: dict                      # minutes per band
    hit_max: int = 4
    min_easy_share: float = 0.0       # 0..1, share of total minutes
    max_hard_share: float = 1.0       # 0..1, share of total minutes, z5plus
    adjacent: list | None = None      # pairs (i, j) that may not both be hard


def solve(problem: WeekProblem, time_limit_s: float = 2.0) -> list[int] | None:
    """Index of the chosen candidate per slot, or None if unsolvable."""
    try:
        import numpy as np
        from scipy.optimize import milp, LinearConstraint, Bounds
    except Exception:
        return None

    slots = problem.slots
    if not slots or any(not c for c in slots):
        return None

    # Variable layout: all x[i,j] first, then u[b] and o[b] for each band.
    idx: list[tuple[int, int]] = [(i, j) for i, cs in enumerate(slots)
                                  for j in range(len(cs))]
    # Position lookup, precomputed. Calling idx.index() inside the constraint
    # loops made matrix assembly O(n^2) and cost 900 ms on a 7x25 week -- more
    # than the solve itself.
    pos = {p: v for v, p in enumerate(idx)}
    nx = len(idx)
    nb = len(BANDS)
    n = nx + 2 * nb
    u0, o0 = nx, nx + nb

    # Two extra variables for the total-volume deviation.
    uT, oT = n, n + 1
    n += 2

    total_target = sum(float(problem.target.get(b, 0.0) or 0.0) for b in BANDS)
    shares = {b: (float(problem.target.get(b, 0.0) or 0.0) / total_target
                  if total_target > 0 else 0.0) for b in BANDS}

    c = np.zeros(n)
    for k, _b in enumerate(BANDS):
        c[u0 + k] = 1.0
        c[o0 + k] = 1.0
    # Volume matters, but a minute of it is worth less than a minute of
    # misplaced intensity: _VOLUME_WEIGHT is per minute against a share
    # deviation also measured in minutes.
    c[uT] = _VOLUME_WEIGHT
    c[oT] = _VOLUME_WEIGHT

    A: list = []
    lo: list = []
    hi: list = []

    # One workout per slot.
    for i, cs in enumerate(slots):
        row = np.zeros(n)
        for j in range(len(cs)):
            row[pos[(i, j)]] = 1.0
        A.append(row); lo.append(1.0); hi.append(1.0)

    # Band balance IN SHARE SPACE: delivered[b] - share[b]*total = o[b] - u[b].
    for k, b in enumerate(BANDS):
        row = np.zeros(n)
        for v, (i, j) in enumerate(idx):
            cand = slots[i][j]
            row[v] = cand.band(b) - shares[b] * cand.minutes
        row[o0 + k] = -1.0
        row[u0 + k] = 1.0
        A.append(row); lo.append(0.0); hi.append(0.0)

    # Total volume: total - oT + uT = target_total.
    row = np.zeros(n)
    for v, (i, j) in enumerate(idx):
        row[v] = slots[i][j].minutes
    row[oT] = -1.0
    row[uT] = 1.0
    A.append(row); lo.append(total_target); hi.append(total_target)

    # Hard-session cap -- recovery, not time.
    row = np.zeros(n)
    for v, (i, j) in enumerate(idx):
        row[v] = 1.0 if slots[i][j].is_hard else 0.0
    A.append(row); lo.append(0.0); hi.append(float(problem.hit_max))

    # 48 hours between hard days: no two adjacent slots both hard.
    for (i, j) in (problem.adjacent or []):
        if i >= len(slots) or j >= len(slots):
            continue
        row = np.zeros(n)
        for v, (a, b_) in enumerate(idx):
            if a in (i, j) and slots[a][b_].is_hard:
                row[v] = 1.0
        A.append(row); lo.append(0.0); hi.append(1.0)

    # Safety rails, as constraints rather than as tests that fail later.
    if problem.min_easy_share > 0:
        row = np.zeros(n)
        for v, (i, j) in enumerate(idx):
            cand = slots[i][j]
            row[v] = cand.band("z1z2") - problem.min_easy_share * cand.minutes
        A.append(row); lo.append(0.0); hi.append(np.inf)
    if problem.max_hard_share < 1.0:
        row = np.zeros(n)
        for v, (i, j) in enumerate(idx):
            cand = slots[i][j]
            row[v] = cand.band("z5plus") - problem.max_hard_share * cand.minutes
        A.append(row); lo.append(-np.inf); hi.append(0.0)

    integrality = np.zeros(n)
    integrality[:nx] = 1
    bounds = Bounds(lb=np.zeros(n),
                    ub=np.concatenate([np.ones(nx), np.full(n - nx, np.inf)]))
    try:
        res = milp(c=c, constraints=LinearConstraint(np.array(A), lo, hi),
                   integrality=integrality, bounds=bounds,
                   options={"time_limit": time_limit_s, "presolve": True})
    except Exception:
        return None
    if not getattr(res, "success", False) or res.x is None:
        return None

    out = [0] * len(slots)
    for v, (i, j) in enumerate(idx):
        if res.x[v] > 0.5:
            out[i] = j
    return out
