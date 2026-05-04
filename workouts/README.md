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

Three sources contribute to the library; all files are interchangeable
at the file level (they all conform to the standard Zwift ZWO schema).

1. **Domestique Library generated** — `<author>Domestique Library</author>`.
   Produced by scripts in `scripts/`:
     - `scripts/generate_gap_workouts.py` — fills thin categories
       (pyramids, short VO2, short threshold, over-unders, neuromuscular
       sprints, short sweet spot). Structure chosen from published
       exercise-physiology protocols.
     - `generate_ftp_workouts.py` (repo root, older) — base FTP suite.

2. **Scraped and re-authored** — reconstructions of publicly-viewable
   workout structures on whatsonzwift.com. The scraper
   (`scripts/scrape_whatsonzwift.py`) infers interval data from the
   rendered visual graph only; it never hits any download/`.zwo`
   endpoint. Only the factual interval numbers (unprotectable under
   Feist v. Rural) are read. Every scraped file is re-authored: `<name>`
   and `<description>` are regenerated from structure, `<author>` is
   set to `Domestique Library`, and all `<textevent>`, `<TextNotification>`,
   `<image>`, `<video>` children are stripped. See
   `workouts/.scrape_progress.json` for the list of URLs processed.

3. **Imported from permissive GitHub repos** —
   `scripts/import_github_workouts.py` imports from:
     - `macgrrl/zwift-workouts` (Unlicense / public domain)
     - `michaelahlers/michaelahlers-zwift-workouts` (MIT)
   All imports are re-authored using the same strip rules as scraped
   files. Provenance (source repo, original filename, license) is
   recorded in `workouts/.github_imports_manifest.json`.

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

Option A — run a generator script:
```sh
python3 scripts/generate_gap_workouts.py
python3 scripts/scrape_whatsonzwift.py --max-duration 60
python3 scripts/import_github_workouts.py
```
Each script is idempotent: it writes only new structure hashes and
updates `workouts/.structure_index.json` in place.

Option B — drop a hand-authored `.zwo` into `workouts/` matching the
filename convention above. After dropping a file, run:
```sh
python3 scripts/dedupe_zwo_library.py --index workouts/
```
to refresh the structure index.

## Hard rules for new files

- `<author>` must be exactly `Domestique Library`.
- No `<textevent>`, `<TextNotification>`, `<image>`, `<video>` children.
- Minimum duration 5 minutes.
- No filenames containing coach names or branded terms (e.g. no
  `emily_`, `jon_`, `sufferfest_`). Published-protocol names are
  retained (`helgerud`, `ronnestad`, `billat`, `coggan`, `tabata`,
  `seiler`) — these are scientific attributions, not brands.

## License

Workout *interval structures* are uncopyrightable facts (Feist v. Rural
Telephone, 499 U.S. 340, 1991). Original `<name>` and `<description>`
prose authored by Domestique Library is released under Apache-2.0.
Imported GitHub content is retained under its original license (see
manifest).
