# Workout Library Sources

Domestique ships a library of ZWO workout files. Each `.zwo` is an XML
description of an interval set (warm-up, intervals, recovery, cool-down).
This document explains where those files come from.

---

## What we include in Domestique

### 1. Procedurally generated — primary source (author: Domestique Library)

Almost the entire shipped library is authored in-house by the workout
generators. They enumerate interval structures (reps × on / off durations ×
%FTP) per category — recovery, endurance, tempo, sweet-spot, threshold, VO2,
anaerobic, sprints, over-under, pyramid — and emit ZWO from templates. Every
file carries `<author>Domestique Library</author>` and a description that is a
factual summary of the structure. The content is original to Domestique.

### 2. GitHub imports — MIT / Unlicense only

A small number of workouts come from two public repositories whose licenses
explicitly permit redistribution and modification:

- `macgrrl/zwift-workouts` — **Unlicense** (public-domain dedication).
- `michaelahlers/michaelahlers-zwift-workouts` — **MIT**.

The importer dedupes against the existing library by structure hash,
normalizes `<author>` to `Domestique Library`, regenerates `<name>` and
`<description>` from the structure, and strips any coach-cue text events. The
original license text and upstream URLs are preserved in
`workouts/.github_imports_manifest.json`.

---

## Sources to avoid

Domestique does not include — and does not accept contributions of — workout
content from commercial or proprietary catalogues:

- **Zwift built-in workouts** — Zwift EULA OEM content. Off-limits.
- **Tacx proprietary workout library** — Garmin proprietary binary format,
  commercial OEM content.
- **TrainerRoad / TrainingPeaks / Wahoo SYSTM** — authenticated, IP-enforced
  workout content. GitHub mirrors that re-host these (e.g. TrainerRoad- or
  Sufferfest-derived files) are the same trap and are excluded.

## Alternative sources for your own use

If you want to browse more workouts yourself:

- **TrainerDay (trainerday.com)** — 40,000+ public community workouts,
  API-accessible, ZWO export supported. Their ToS permits free use of public
  workouts but restricts bulk harvesting; contact them in writing for bulk-sync
  permission.
- **zwofactory.com** — user-submitted workout templates (mixed provenance);
  cherry-pick individual workouts you like rather than bulk-importing the set.

---

## Attribution

Imported MIT / Unlicense files retain their original license headers, and the
importer writes a manifest at `workouts/.github_imports_manifest.json`
recording the source repository URL + commit SHA, original filename + license,
import date, and our normalized filename.
