"""The week assignment, as a constrained optimisation rather than a nudge.

What a solver buys over the greedy repair it replaces is not just a better
answer: it is that the SAFETY RAILS BECOME CONSTRAINTS. The greedy version
could only measure the rails after the fact, and the plan-property tests caught
it breaching them. Here they are in the model, so a returned solution satisfies
them by construction or there is no solution.

These tests check the formulation, not the solver -- HiGHS is not ours to test.
Every one asserts a property the model claims to guarantee.
"""
import os
import pathlib
import random
import sys
import unittest

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import week_solver as ws  # noqa: E402


def _slots(rng, n_slots=7, n_cand=20, hard_p=0.35):
    out = []
    for i in range(n_slots):
        cs = [ws.Candidate(key=("rest", i), bands={b: 0.0 for b in ws.BANDS})]
        for j in range(n_cand):
            hard = rng.random() < hard_p
            cs.append(ws.Candidate(
                key=(i, j),
                bands={"z1z2": rng.uniform(25, 90), "z3": rng.uniform(0, 30),
                       "z4": rng.uniform(0, 20),
                       "z5plus": rng.uniform(2, 20) if hard else 0.0},
                is_hard=hard))
        out.append(cs)
    return out


def _problem(rng, **kw):
    base = dict(slots=_slots(rng),
                target={"z1z2": 400, "z3": 25, "z4": 20, "z5plus": 45},
                hit_max=4, min_easy_share=0.55, max_hard_share=0.18,
                adjacent=[(i, i + 1) for i in range(6)])
    base.update(kw)
    return ws.WeekProblem(**base)


def _delivered(problem, pick):
    return {b: sum(problem.slots[i][pick[i]].band(b)
                   for i in range(len(problem.slots))) for b in ws.BANDS}


def _feasible(problem, pick):
    """Does this assignment satisfy the model's constraints?

    The greedy walk below has to be held to them too. Comparing a CONSTRAINED
    optimum against an UNCONSTRAINED greedy walk is not a comparison: greedy
    simply reaches assignments the model forbids -- over the hard-session cap,
    hard days back to back, through the easy floor -- and arrives cheaper by
    cheating. Measured, that is exactly what made the solver look like it lost.
    """
    hard = [problem.slots[i][j].is_hard for i, j in enumerate(pick)]
    if sum(hard) > problem.hit_max:
        return False
    for (i, j) in (problem.adjacent or []):
        if hard[i] and hard[j]:
            return False
    d = _delivered(problem, pick)
    total = sum(d.values())
    if total > 0:
        if d["z1z2"] / total < problem.min_easy_share - 1e-9:
            return False
        if d["z5plus"] / total > problem.max_hard_share + 1e-9:
            return False
    return True


def _cost(problem, pick):
    """The solver's OWN objective: deviation in share space plus a volume term.

    Scoring the comparison against absolute band minutes instead compared the
    solver to greedy on a metric the solver was not optimising, and the solver
    duly "lost". A comparison has to be in the objective under test.
    """
    d = _delivered(problem, pick)
    total_target = sum(problem.target.values())
    shares = {b: (problem.target[b] / total_target if total_target else 0.0)
              for b in ws.BANDS}
    total = sum(d.values())
    share_dev = sum(abs(d[b] - shares[b] * total) for b in ws.BANDS)
    return share_dev + ws._VOLUME_WEIGHT * abs(total - total_target)


class TheModelRespectsItsConstraints(unittest.TestCase):
    def setUp(self):
        self.solved = []
        for seed in range(6):
            p = _problem(random.Random(seed))
            pick = ws.solve(p)
            if pick is not None:
                self.solved.append((p, pick))

    def test_it_solves_at_all(self):
        self.assertGreaterEqual(len(self.solved), 5, "the model is over-constrained")

    def test_exactly_one_workout_per_slot(self):
        for p, pick in self.solved:
            self.assertEqual(len(pick), len(p.slots))
            for i, j in enumerate(pick):
                self.assertTrue(0 <= j < len(p.slots[i]))

    def test_the_hard_session_cap_holds(self):
        for p, pick in self.solved:
            n = sum(1 for i, j in enumerate(pick) if p.slots[i][j].is_hard)
            self.assertLessEqual(n, p.hit_max)

    def test_no_two_hard_days_are_adjacent(self):
        """48 hours between hard sessions, as a constraint rather than a hope."""
        for p, pick in self.solved:
            for (i, j) in p.adjacent:
                self.assertFalse(p.slots[i][pick[i]].is_hard
                                 and p.slots[j][pick[j]].is_hard,
                                 f"hard on both {i} and {j}")

    def test_the_easy_floor_holds(self):
        for p, pick in self.solved:
            d = _delivered(p, pick)
            total = sum(d.values())
            if total <= 0:
                continue
            self.assertGreaterEqual(d["z1z2"] / total, p.min_easy_share - 1e-6)

    def test_the_intensity_ceiling_holds(self):
        for p, pick in self.solved:
            d = _delivered(p, pick)
            total = sum(d.values())
            if total <= 0:
                continue
            self.assertLessEqual(d["z5plus"] / total, p.max_hard_share + 1e-6)


class ItBeatsTheGreedyItReplaces(unittest.TestCase):
    def test_the_solution_is_no_worse_than_a_greedy_walk(self):
        """The claim that justifies the change. Greedy here is the same move
        set the hand-rolled repair used: start from a random assignment and
        take the best single-slot improvement until none helps."""
        wins = ties = 0
        for seed in range(8):
            rng = random.Random(100 + seed)
            p = _problem(rng)
            opt = ws.solve(p, time_limit_s=10.0)
            if opt is None:
                continue
            cur = [0] * len(p.slots)          # all-rest: always feasible
            for _ in range(40):
                base, best = _cost(p, cur), None
                for i in range(len(p.slots)):
                    for j in range(len(p.slots[i])):
                        if j == cur[i]:
                            continue
                        trial = list(cur); trial[i] = j
                        if not _feasible(p, trial):
                            continue
                        c = _cost(p, trial)
                        if c < base - 1e-9 and (best is None or c < best[0]):
                            best = (c, i, j)
                if best is None:
                    break
                cur[best[1]] = best[2]
            if _cost(p, opt) < _cost(p, cur) - 1e-9:
                wins += 1
            elif abs(_cost(p, opt) - _cost(p, cur)) <= 1e-9:
                ties += 1
            self.assertLessEqual(_cost(p, opt), _cost(p, cur) + 1e-6,
                                 f"seed {seed}: the solver lost to greedy")
        self.assertGreaterEqual(wins + ties, 6)


class ItFailsSafely(unittest.TestCase):
    def test_rails_that_no_training_week_can_meet_produce_rest_not_a_breach(self):
        """The share constraints are ratios, so a zero-volume week satisfies
        them vacuously -- and that is the RIGHT answer, not a hole. Asked for a
        distribution no available session can produce, the model prescribes
        rest rather than handing back a week that breaks the floor. Winding
        down to nothing is a legitimate outcome; breaching is not.
        """
        p = _problem(random.Random(1), min_easy_share=0.999, max_hard_share=0.001)
        pick = ws.solve(p)
        self.assertIsNotNone(pick)
        d = _delivered(p, pick)
        total = sum(d.values())
        if total > 0:                       # if it did train, it obeyed the rails
            self.assertGreaterEqual(d["z1z2"] / total, p.min_easy_share - 1e-6)
            self.assertLessEqual(d["z5plus"] / total, p.max_hard_share + 1e-6)

    def test_a_genuinely_infeasible_model_returns_none(self):
        """None means 'keep what you had'. A solver that cannot solve must not
        hand back something worse than the heuristic it replaced."""
        p = _problem(random.Random(1), hit_max=-1)
        self.assertIsNone(ws.solve(p))

    def test_an_empty_slot_list_returns_none(self):
        self.assertIsNone(ws.solve(ws.WeekProblem(slots=[], target={})))
        self.assertIsNone(ws.solve(ws.WeekProblem(slots=[[]], target={})))

    def test_the_same_problem_gives_the_same_answer(self):
        # Determinism, not speed: under a loaded gate (xdist -n 4) the default
        # 2 s limit can expire on one of the two solves, which then returns
        # None by design ("fails safely") and the comparison is meaningless.
        p = _problem(random.Random(7))
        self.assertEqual(ws.solve(p, time_limit_s=60.0), ws.solve(p, time_limit_s=60.0))

    def test_a_rest_only_week_is_representable(self):
        """Every slot can take the zero candidate, which is how the model says
        'the week's work is already done' -- the wind-down case."""
        rng = random.Random(3)
        p = _problem(rng, target={b: 0.0 for b in ws.BANDS},
                     min_easy_share=0.0, max_hard_share=1.0)
        pick = ws.solve(p)
        self.assertIsNotNone(pick)
        self.assertAlmostEqual(sum(_delivered(p, pick).values()), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
