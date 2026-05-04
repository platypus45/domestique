# Virtual Routes

500 procedurally generated cycling routes across 3 worlds:

- **Blue Ridge** — rolling hills and mixed terrain
- **Iron Pass** — mountain climbs and summit finishes
- **Desert Loop** — flat TT courses and light rollers

## Generation method

Routes are synthesized using deterministic Perlin-style noise with
seeded RNG. No third-party route data of any kind was used. Each
route is reproducible by the seed derived from `(world, index)`.

Route distribution (distances and types) was informed by generic
cycling variety statistics.

## Regeneration

```bash
python3 generate_procedural_routes.py courses/virtual
```

## License

Apache-2.0 — free to use, modify, redistribute.
