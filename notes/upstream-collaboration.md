# Upstream collaboration note (2026-09-16)

Copied from https://github.com/platypus45/domestique/issues/13 so it is readable here as well; the conversation lives there.

---

Hi, platypus45 here, Domestique upstream.

It is genuinely good to see the work continue at this depth. I read refactor/backend-architecture and the notes; the diagnosis in overhaul-plan.md and the six-lens audit is better than anything upstream had, and I checked several findings against clean-main: the second planner behind the home target (HTTP-2), regenerate and recalculate calling generate_phases without recent load (DUP-1), and the pace attributes on the six FTP-test files that make intervals.icu return 422 on a calendar push. All real. Thank you for that.

I would like to bring this upstream together with you rather than let the two trees drift. Here is what I propose, and what I have already done on our side.

**Your two rules: adopted.**

- P8, reads do not write. The 3.11.5 plan self-heal moves off GET /api/plan; it will run at startup and after the plan-writing POSTs, the moments a session can lose its file. Your reads-do-not-write test becomes the gate for it.
- D6, a missed or dismissed session costs nothing. Our accounting already reads done load from rides only, and the 48 h rule and the hard cap count done hard days, never missed ones. The easy-volume recycle from 3.11.5 goes: Mujika is right that a missed session is not a debt. One bounded re-owe of a missed hard session stays, which is what your auto-move does as well.

**The merge.** I am doing it myself, as one pull request on a branch in this repo, resolved under a written contract (twelve rules, P8 and D6 among them), so you can review the resolutions in your own code before anything lands. Your history stays intact so your later work applies cleanly. The PR follows within the next days; I will link it here. After that, your steps land as your own pull requests, in the order you gated them, and releases stay on our cadence with the macOS, Windows and Linux builds and the tag CI.

**Working together.** You have a collaborator invite so you can push straight to the merge branch and open pull requests here without going through the fork. clean-main now requires a pull request with one approving review before anything lands, and release tags stay with the owner. The agreement, written down: nothing lands on clean-main and nothing is released without both of us having looked at it. Same rule for my merge PR.

**One thing to hold back:** the FIT step-duration change (fc5f1edb). With fit_tool 0.9.15, the version the app ships, duration_value = seconds x 1000 decodes as a 300 s step in fitparse, and duration_time = 300 decodes as 0.3 s. If your fit_tool version behaves differently, worth pinning down together before either of us ships it. Happy to share the check.

Glad to have you on this.
