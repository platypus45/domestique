# docs/

Documentation that doesn't fit at repo root: architecture notes,
science deep-dives, build guides, library provenance, and historical
planning artefacts.

| File | Category | One-liner |
|---|---|---|
| `SYSTEM.md` | Architecture & system | High-level system architecture (modules, data flow, persistence). |
| `DEBUGGING.md` | Architecture & system | Common diagnostic commands + log layout. |
| `SCIENCE_REVIEW.md` | Science & research | Citation-by-citation review of the seven injury guardrails and the periodisation model. |
| `RESEARCH_TRAINING_PLANNER.md` | Science & research | Source notes for the planner: Seiler / Mujika / Stoggl / Coggan / Friel / Allen. |
| `windows_build.md` | Build / packaging | Path to a Windows `.exe` (PyInstaller `--onedir` + Inno Setup + GitHub Actions on `windows-latest`, ~4 days). |
| `workout_sources.md` | Library provenance | Where the workout library comes from (procedurally generated + permissively-licensed GitHub imports). |
| `cycling_apps.md` | Library provenance | Comparison of free cycling apps that accept ZWO/FIT (Golden Cheetah, MyWhoosh, Tacx Training). |
| `TRAINING_SCREEN_PLAN.md` | Planning artefacts | Wireframe + decisions that produced the Plan tab in v4.6+. |
| `workout_analysis.md` | Planning artefacts | Original library audit: which categories were over- vs under-represented before procgen filled the gaps. |

These files have no inbound links from `app.py` / `launcher.py` / any
build script. They are read by humans, not the app.

The canonical user-facing top-level docs (`README.md`, `CHANGELOG.md`,
`LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `COURSES_LICENSE.md`,
`TRADEMARKS.md`, `CLAUDE.md`) stay at repo root by open-source
convention.
