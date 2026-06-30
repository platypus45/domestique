# CLAUDE.md

> Source: [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT). Derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

**The ladder (ponytail).** Before writing code, stop at the first rung that holds: (1) does it need to exist? (YAGNI) → (2) already in this codebase? reuse it → (3) stdlib does it? → (4) native platform feature? → (5) already-installed dependency? → (6) one line? → (7) only then the minimum that works. Run it *after* understanding the problem, never instead of — read the code the change touches and trace the real flow first.

**Bug fix = root cause, not symptom.** A report names a symptom. Grep every caller of the function you touch and fix the shared function once, rather than patching only the path the ticket names (which leaves sibling callers broken).

**Mark deliberate shortcuts** with a `ponytail:` comment that names the ceiling and upgrade path — e.g. `# ponytail: global lock, per-account if throughput matters`.

Never lazy about: understanding the problem, validation at trust boundaries, error handling that prevents data loss, security, accessibility, or anything explicitly requested. For deeper enforcement on a given task, invoke the `/ponytail` skill (lite/full/ultra) — full text in `.claude/skills/ponytail/SKILL.md`.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

## Project-Specific Guidelines (Domestique)

These guidelines layer on top of the four principles above:

- **Multi-wave agent pattern:** large features go through Wave 0 research → Wave 1/2 grill+impl → Wave 3 QA → Wave 4 fix-forward → Wave 5 ship+tag+DMG. Don't dispatch agents ad-hoc.
- **Sparse coordinator output:** when an agent reports back, give the user a one-line status — never echo the agent's full report. Full detail goes to `/tmp/*.md`.
- **MASTER_DECISIONS docs override per-domain plans on conflict.** Write to `/tmp/MASTER_DECISIONS_*.md` and force agents to read it first.
- **File ownership contracts:** when parallel agents touch overlapping areas, lock who owns which file BEFORE Wave 2. No two agents edit the same file in the same wave.
- **Tests must pass at each wave boundary.** Pytest baseline at `clean-main` HEAD must not regress.
- **Adversarial role split (loops):** the implementer never grades its own work — a separate evaluator must try to *prove* the change is broken (run it, read the diff). Self-grading → sycophancy → slop.
- **Contract before code (loops):** lock a checklist of *testable* assertions (the IP / `MASTER_DECISIONS`, grilled) BEFORE implementing; the contract is what gets graded, and ≤10 assertions usually means the grill rubber-stamped.
- **Restart over patch (loops):** when a run goes sideways, revert + redo rather than patch-the-patch; escalate to the user only when the *contract* is wrong, not when a build is.
- **Debug from traces; prune the harness (loops):** read the transcript / `*.jsonl` to find where judgment diverged before re-running; re-read CLAUDE.md + skills each model release and cut what the model now does for free. The bottleneck always moves (coding → planning → verification → taste). Full discipline: the `/loops` skill.
- **Trainer subsystem is gone since v4.0.0-alpha.** Don't add `bleak`, `pycycling`, `_on_real_trainer_*`, FTMS, ERG, FE-C, or anything that talks to a hardware trainer. Domestique is planner + library + post-ride viewer.
- **Workout classification is content-based, not filename-based** (since v4.1.2). Read `workouts/.content_classification.json` for the canonical type. Filename rules are fallback only.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
