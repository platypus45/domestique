# Workouts

ZWO (Zwift workout) library. Structured cycling workouts used by the
Domestique planner. Each file encodes a sequence of power-target
segments as fractional FTP (0.65 = 65% FTP).

## File count by category

Counts are approximate (regenerate with `ls workouts/<type>_*.zwo | wc -l`):

| Category | Prefix | Purpose |
|---|---|---|
| recovery    | `recovery_`    | ≤55% FTP, active recovery |
| endurance   | `endurance_`, `z2_` | 56-75% FTP, long aerobic base |
| tempo       | `tempo_`       | 76-87% FTP |
| sweet spot  | `sweetspot_`, `sweet_spot_` | 88-94% FTP |
| threshold   | `threshold_`, `supra_` | 95-105% FTP |
| VO2max      | `vo2_`, `vo2max_` | 106-120% FTP |
| anaerobic   | `anaerobic_`   | >120% FTP, <60s on |
| sprints     | `sprints_`     | all-out neuromuscular, 6-20s |
| over-under  | `over_under_`  | alternating sub/supra threshold |
| pyramid     | `pyramid_`     | ladder / reverse-ladder structures |
| ftp test    | `ftp_test_*`   | Coggan 20-min + Ramp test |
| intervals (generic) | `intervals_` | legacy generator output |

## Sources

Two sources contribute to the library; all files conform to the standard
Zwift ZWO schema and are interchangeable at the file level.

1. **Domestique Library generated** — `<author>Domestique Library</author>`,
   the primary source. Produced by the generator scripts: structures are
   chosen from published exercise-physiology protocols (recovery, endurance,
   tempo, sweet-spot, threshold, VO2, anaerobic, sprints, over-unders,
   pyramids) and emitted from templates with original prose.

2. **Imported from permissive GitHub repos** —
     - `macgrrl/zwift-workouts` (Unlicense / public domain)
     - `michaelahlers/michaelahlers-zwift-workouts` (MIT)
   Imports are re-authored to `<author>Domestique Library</author>` with
   regenerated names/descriptions. Provenance (source repo, original
   filename, license) is recorded in `workouts/.github_imports_manifest.json`.

Nothing is scraped or reconstructed from any third-party workout site.

## Filename convention

```
<type>_<structure>_<duration>min.zwo
```

Examples:
- `vo2max_helgerud_4x4min_60min.zwo` — 4×4 min VO2max (Helgerud 2007)
- `threshold_2x20min_75min.zwo` — 2×20 min threshold, 75 min total
- `sweetspot_3x15min_75min.zwo` — 3×15 min sweet spot
- `z2_endurance_120min.zwo` — 2-hour Z2 endurance
- `pyramid_ladder_1-2-3-4-3-2-1_42min.zwo` — 1-2-3-4-3-2-1 pyramid

When two new workouts would share a filename but differ in structure,
the second gets `_v2`, `_v3`, etc. appended. Two files with identical
structure hash are never both written (dedupe via
`scripts/dedupe_zwo_library.py`).

## How to add new workouts

Option A — run a generator script, then refresh the index:
```sh
python3 scripts/dedupe_zwo_library.py --index workouts/
```
Generators are idempotent: they write only new structure hashes and
update `workouts/.structure_index.json` in place.

Option B — drop a hand-authored `.zwo` into `workouts/` matching the
filename convention above, then run the dedupe/index command above.

## Hard rules for new files

- `<author>` must be exactly `Domestique Library`.
- No `<textevent>`, `<TextNotification>`, `<image>`, `<video>` children.
- Minimum duration 5 minutes.
- No filenames containing coach names or branded terms (e.g. no
  `emily_`, `jon_`, `sufferfest_`). Published-protocol names are
  retained (`helgerud`, `ronnestad`, `billat`, `coggan`, `tabata`,
  `seiler`) — these are scientific attributions, not brands.

## License

Original `<name>` and `<description>` prose authored by Domestique Library
is released under Apache-2.0. Imported GitHub content is retained under its
original license (see the imports manifest).
