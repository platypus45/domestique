# scripts/

One-off generators and library-maintenance tools. None of
these are imported by `app.py` / `launcher.py` — they run by hand,
typically once per release, to (re)build the workout library or
audit it.

**Hard rule:** no module here is `import`ed by the running app. If
you find yourself importing a `scripts/` module from `app.py` or
another root module, move that module to repo root first.

## Run

All scripts are invoked from repo root:

```sh
python scripts/<name>.py
```

A few have CLI flags (`--dry-run`, `--max-duration`, `--index`); the
rest read defaults at the top of the file.

## What lives here

### Library generators

| Script | Purpose |
|---|---|
| `generate_ftp_workouts.py` | Base FTP-test suite (Coggan 20-min + Ramp). |
| `generate_procedural_routes.py` | Procedural virtual routes (terrain × distance × climbing matrix). |
| `generate_route_profiles.py` | Per-route elevation profile JSONs consumed by the dashboard route picker. |
| `generate_gap_workouts.py` | Fills under-represented categories (pyramids, short VO2, short threshold, over-unders, neuromuscular sprints, short sweet spot). |

### Importers

| Script | Purpose |
|---|---|
| `import_github_workouts.py` | Imports from `macgrrl/zwift-workouts` (Unlicense) and `michaelahlers/michaelahlers-zwift-workouts` (MIT). Tracks provenance in `workouts/.github_imports_manifest.json`. |

### Library maintenance / audits

| Script | Purpose |
|---|---|
| `dedupe_zwo_library.py` | Hash-based dedupe; updates `workouts/.structure_index.json`. |
| `classify_library_content.py` | Content-based ZWO classifier (since v4.1.2). Writes `workouts/.content_classification.json`. |
| `audit_tempo_workouts.py` | Audits `workouts/tempo_*.zwo` for shape consistency (steady vs progression vs ramping vs mixed). |
| `regen_tempo_steady.py` | Rewrites tempo files flagged as `ramping_undesired` into pure SteadyState. |
| `library_overhaul_v46.py` | Library overhaul v4.6.0 (one-shot). |
| `library_name_audit_v1.py` | v1.0.0 name-vs-structure mismatch audit. |
| `reclassify_mixed_v461.py` | Promotes mixed-class ZWO files into reachable buckets. |
| `osm_surface_mapper.py` | Per-segment surface classification (asphalt / gravel / cobble / unknown) from OSM Overpass for real-world routes. |

### Relocation history

`generate_ftp_workouts.py`, `generate_procedural_routes.py`, and
`generate_route_profiles.py` were relocated from repo root as part of
the v1.0 layout cleanup. The library-maintenance scripts were already
under `scripts/`.
