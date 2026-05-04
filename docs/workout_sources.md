# Workout Library Sources

Domestique v4.0.0-alpha ships a library of ZWO workout files. Each `.zwo`
is an XML description of an interval set (warm-up, intervals, recovery,
cool-down). This document enumerates where those files come from, what
we skip, and why.

---

## What we include in Domestique

### 1. Procedurally generated — primary source (author: Domestique Library)

Most of the shipped library is authored in-house by `generate_gap_workouts.py`
and `generate_ftp_workouts.py`. The generators enumerate interval structures
(reps × on / off durations × %FTP) per category — recovery, endurance,
tempo, sweet-spot, threshold, VO2, anaerobic, sprints, over-under, pyramid —
and emit ZWO from templates. Every file has `<author>Domestique Library</author>`
and a description that is a factual summary of the structure.

Zero copyright risk: interval numbers are facts, authored prose is ours.

### 2. GitHub imports — MIT / Unlicense only

We import workouts from two public repositories whose licenses explicitly
permit redistribution and modification:

- `macgrrl/zwift-workouts` — 15 files, **Unlicense** (public-domain dedication).
- `michaelahlers/michaelahlers-zwift-workouts` — 16 files, **MIT**.

The importer (`scripts/import_github_workouts.py`) clones, dedupes against
the existing library by structure hash, normalizes `<author>` to
`Domestique Library`, regenerates `<name>` and `<description>` from the
structure, and strips any `<textevent>` / `<TextNotification>` coach cues.
The original license text and upstream URLs are preserved in
`workouts/.github_imports_manifest.json`.

Copyright posture: interval numbers and section types are carried over
under the permissive source license; all creative layers (name,
description, coach cues) are replaced with our own.

### 3. whatsonzwift reconstructions — visual-graph inference only

For workouts that appear on whatsonzwift.com's per-workout HTML detail
pages (the ones rendered as a power-over-time SVG graph), we read the
publicly-displayed facts — durations, power-target percentages, segment
types — by parsing the rendered `<svg>` / inline JS data / block labels.
We then author our own ZWO file from scratch using those facts.

**We never hit any `/download` or `/workout-files-custom` endpoint.** We
do not fetch the site's ZWO file product. We read what the HTML page
displays to a human visitor and reconstruct. Rate-limited 0.5 s between
requests; identifying User-Agent; HTTP 429 backoff; checkpointed progress.

On our output file: `<author>Domestique Library</author>`;
`<name>` and `<description>` are regenerated from the structure
(e.g. `"4 × 8-min intervals at 100% FTP, 3-min recovery at 55%"`),
never copied from the source page; all `<textevent>` / `<TextNotification>`
coach cues are stripped.

---

## Alternative sources for your own use

If you want to browse more workouts yourself, these are the places:

- **TrainerDay (trainerday.com)** — 40,000+ public community workouts,
  API-accessible, ZWO export supported. Their ToS explicitly permits
  "free use of any public workouts" but restricts bulk harvesting;
  contact them in writing for a bulk-sync permission. Best single source
  if you want a very large catalogue.
- **zwofactory.com** — user-submitted workout templates (mixed
  provenance). Cherry-pick individual workouts you like; do not bulk-import
  the full template set — it contains Zwift OEM content under author
  attributions like "Zwift" and "Marco Pinotti".
- **whatsonzwift.com directly** — their intended flow is browse their
  catalogue in a web browser and click Download on per-workout pages that
  have a Download button (custom user submissions only; built-in Zwift
  plans render as HTML). This is the site's own product and is fine to
  use in the way they designed it.

---

## Sources to avoid

- **Zwift built-in workouts** (`<ZwiftInstall>/data/workouts/`) — Zwift
  EULA covers these as OEM content. Explicitly off-limits.
- **Tacx proprietary workout library** (Tacx Training app) — Garmin
  proprietary binary format, commercial OEM content under Garmin EULA.
- **TrainerRoad / TrainingPeaks / Wahoo SYSTM** — all sit behind
  authentication walls and enforce IP on their workout content.
  `sbuscher/zwift-workouts` on GitHub is explicitly "TrainerRoad
  workouts converted to .zwo" and is a trap for the same reason.
  `bdcheung/zwift_workouts` ships "Sufferfest -" prefixed files lifted
  from Wahoo Sufferfest.

---

## Copyright

Interval numbers (durations, power percentages, repeat counts) are
uncopyrightable facts (Feist Publications v Rural Telephone, 499 U.S. 340,
1991). Names, descriptions, and coach cues carry author copyright — we
strip those and author our own.

## Legal stance on visual-graph scraping

Reading publicly-displayed facts from a rendered workout page is not the
same as downloading the site's file product. We never hit any
`/workout-files-custom` or `/download` endpoint. The visual-graph
inference pathway extracts only what a human visitor sees on the page
(interval durations + power targets), and the resulting ZWO file is
authored from scratch with our own prose layer.

## Attribution

All imported MIT / Unlicense files retain their original license headers
(either inside the ZWO description or in an adjacent sidecar), and the
importer writes a manifest at `workouts/.github_imports_manifest.json`
recording:

- Source repository URL + commit SHA
- Original filename and license
- Import date
- Our normalized filename

---

*As of 2026-04-24.*
