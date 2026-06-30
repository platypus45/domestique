---
name: loops
description: "Discipline for long-running / multi-wave / multi-day agent loops: separate planner/generator/evaluator roles, negotiate a testable contract first, keep state on disk, let the loop restart, score taste with a rubric, read the traces, and prune the harness. Invoke when orchestrating a big feature across many agent turns/waves, not for a single edit (use ponytail for that)."
source: Andrej Karpathy, LOOPS.md (v060726) — personal-use field notes, rephrased + condensed
---

# Loops — running agents that go for hours/days

> Vendored from Andrej Karpathy's *LOOPS.md: Field Notes on Agents That Run for
> Days* (v060726). Orchestration complement to [[ponytail]] (which is per-edit
> discipline) and to CLAUDE.md's multi-wave pattern. The thesis: most agent
> systems die from a weak HARNESS, not a weak model. Short loops, simple state,
> clean contracts — everything else is decoration.

## The five verbs
The loop is: **gather → reason → act → verify → repeat.** Everything below is a
footnote on those. If you're iterating on a single message at 3am, you're still
prompting — close the tab and write the loop.

## 1. Write the loop, not the prompt
The unit of leverage is the *procedure*, not the message. Models are good enough
to follow a procedure unsupervised; invest there.

## 2. Separate the roles
Three roles, three contexts, three system prompts:
- **Planner** — turns a vague human sentence into a sprint spec. Never touches code.
- **Generator** — writes everything. FORBIDDEN to grade its own work.
- **Evaluator** — reads diffs, runs the app (e.g. playwright), and is told from
  message one that the code is broken and its job is to *prove it*.
Mixing roles is the most common failure: the model turns sycophantic the moment it
grades itself, and the loop quietly converges on slop.

## 3. Negotiate the contract first
Before the generator writes a line, it proposes what "done" looks like and the
evaluator pushes back. They argue via markdown on disk until they agree on a
**checklist of testable assertions** (~27 for a small app is reasonable; ≤10 and the
evaluator rubber-stamps). The planner's spec is the boundary; **the contract is what
gets graded.** This is the single change that moves runs from broken demos to
working products.

## 4. Write to disk, not to context
Context windows lie — they compact, rot, and hide what you said an hour ago. A file
doesn't. Keep `feature_list.json`, `progress.md`, `contract.md`, and an append-only
`log.md` (`## [YYYY-MM-DD] op | title` entries). The loop must survive a crash and
resume by reading ~3 files. If you can't describe your state in three files, your
state is too complicated.

## 5. Let the loop restart
The best behavior in current frontier models is willingness to throw everything away
and start over when a run goes sideways — delete at iteration 9, ship at 11. Don't
interrupt it; **the restart is the loop working.** Insert a human only when the
*contract* is wrong, not when the build is.

## 6. Score the subjective
Taste is gradable if you write it down. Use weighted axes (design / originality /
craft / functionality). Calibrate the evaluator on three reference outputs it's told
are good and three it's told are slop. Output a number in [0,1] plus a paragraph on
the gap. The model won't invent taste — it converges toward the taste you described,
so the whole game is writing the rubric carefully enough that converging on it is
what you actually wanted.

## 7. Read the traces
Every real debugging insight about a loop comes from reading the raw transcript, not
running another experiment. Pipe the agent's output to a file, grep for the moment
its judgment diverged from yours, edit the prompt for that exact moment, rerun. Same
muscle as reading a stack trace — except the trace is in English and most of it is
the model talking to itself. Skip this and you're tuning by vibe.

## 8. Delete the harness
The harness exists to compensate for the model; as the model improves, half of what
you wrote last quarter becomes overhead. Context-resetting, sprint decomposition,
scaffolding — each was load-bearing for one model generation and dead weight for the
next. Re-read the harness against every release and delete anything the model now
does for free. A harness that only grows is one you've stopped reading.

## 9. The bottleneck always moves
Coding → planning → verification → taste. You don't finish; you find the next thing
to fix. The point of the loop is to make the next bottleneck visible — if everything
feels smooth, you're not looking carefully enough. Find it, fix it, ship a *smaller*
harness, repeat.

## Domestique mapping (how this is already wired)
- Roles → the multi-wave agent pattern (Wave 0 research → grill → impl → QA → ship).
- Contract → the IP (`IP_*.md`) + `/tmp/MASTER_DECISIONS_*.md`, grilled before code.
- Disk state → `IP_*.md`, per-item commits, the memory dir.
- Restart → fix-forward waves; revert + redo over patching.
- Traces → read agent transcripts + `*.jsonl` rather than re-running blind.
- Delete the harness → re-read CLAUDE.md / skills each model release; cut what's now free.
