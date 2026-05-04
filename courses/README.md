# courses/

Real-world climb library as CRS gradient files. CRS is the format
consumed by Golden Cheetah / RGT / Tacx Desktop App for slope-driven
indoor riding (your trainer follows the gradient profile you chose).

The whole folder ships into the PyInstaller bundle via
`("courses","courses")` in `domestique.spec` `datas=`.

## Layout

Per-region subdirectories. ~160 climbs total covering the European
classics + a handful of regional gravel zones:

```
courses/
├── alps/        — Galibier, Stelvio, Mont Ventoux, Tonale, Gavia, Mollard, ...
├── pyrenees/    — Tourmalet, Aubisque, Marie-Blanque, Hautacam, ...
├── dolomites/   — Pordoi, Sella, Falzarego, Stelvio (east), ...
├── andorra/, austria/, basque/, costa_blanca/, costa_daurada/,
│   flanders/, france_gravel/, germany_gravel/, girona/, gravel/,
│   gravel_europe/, italy_gravel/, lanzarote/, london/, mallorca/,
│   netherlands_gravel/, pyrenees/, richmond/, scotland/, tenerife/,
│   usa_gravel/
├── virtual/     — Procedurally generated climbs (blue_ridge, desert_loop, iron_pass, ...).
│                  Has its own README with the full virtual-route list.
└── other/       — Legacy / uncategorised
```

## Adding a new climb

Drop a GPX into `gpx_sources/<region>/`, then convert from repo root:

```sh
python gpx_to_gc.py
```

`gpx_to_gc.py` lives at repo root (it's imported by `app.py`); it
walks `gpx_sources/` and writes the matching `.crs` next to the
existing files in `courses/<region>/`.

## Licensing

Real-world routes are derived from public elevation data (SRTM /
EU-DEM / Mapbox Terrain). Per-route provenance is in
[`COURSES_LICENSE.md`](../COURSES_LICENSE.md).
