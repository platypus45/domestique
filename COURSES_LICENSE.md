# Course and Route Data Licensing

Routes shipped with Domestique fall into two categories with different
licensing. This document describes both.

## 1. Virtual / Procedural Routes — `courses/virtual/`

Everything under `courses/virtual/` is **procedurally generated** by the
repository's own code. No external data sources are used.

- **License:** Apache-2.0 (same as the rest of the project).
- **Regeneration:** these routes can be regenerated deterministically from
  source using:

  ```bash
  python generate_procedural_routes.py
  ```

- **Modification:** free to modify, redistribute, or use in derivative works
  under the terms of Apache-2.0.

## 2. Real-World Courses — `courses/alps/`, `courses/pyrenees/`, `courses/dolomites/`, etc.

These courses represent **real-world geographic features** (Alpe d'Huez, Col
du Tourmalet, Passo dello Stelvio, etc.).

### Names and Factual Data

The names of real-world climbs, passes, and roads are **public-domain
geographic facts** and are not subject to copyright.

**GPS coordinates and elevation measurements of real geographic features
are also facts** — per *Feist Publications v. Rural Telephone Service* (1991),
factual data (including measurements of physical reality) cannot be
copyrighted. The length, gradient, and elevation of a real road represent
measurements of the physical world, which courts have consistently held to
be facts outside the scope of copyright protection.

This means:
- Route coordinates and elevation profiles of real climbs are facts
- Their accuracy (or lack thereof) does not create copyright
- The selection, arrangement, or presentation of the data may have thin
  compilation copyright, but the underlying facts do not

### Gradient and Elevation Data Provenance

Each course folder documents the provenance of its gradient profile in a
local `README` or `SOURCE` note. The two primary sources are:

#### OpenStreetMap (OSM)

Where elevation segments are derived from OpenStreetMap data, they are used
under the **Open Database License (ODbL) 1.0**.

Attribution: "© OpenStreetMap contributors. Available under the Open Database
License: https://opendatacommons.org/licenses/odbl/1-0/"

Any substantial extract or derivative database built from this OSM-derived
data must itself be distributed under ODbL 1.0.

#### SRTM (Shuttle Radar Topography Mission)

Where elevation data is derived from SRTM datasets published by NASA / USGS,
it is in the **U.S. public domain**. No license restrictions apply, though we
appreciate attribution to USGS / NASA SRTM.

### What Is *Not* Included

- No scraped data from Strava segments.
- No proprietary Zwift, Rouvy, or Wahoo RGT route data.
- No copyrighted photographs or textures; all in-app visuals are either
  generated, Apache-2.0, or individually attributed.

### Bring Your Own GPX

Users are encouraged to import their own GPX files via `gpx_to_gc.py` or the
in-app route importer. Your own GPX recordings are yours to use however you
wish. If you import GPX downloaded from a third-party service, you are
responsible for complying with that service's terms.

## Contributing New Routes

New routes may be contributed under Apache-2.0 provided:

1. The route is either procedurally generated, user-authored, or derived from
   OSM / SRTM / another license-compatible source.
2. The source and its license are documented in a `SOURCE` file alongside the
   route data.
3. The contribution follows [CONTRIBUTING.md](CONTRIBUTING.md).

Routes that appear to be scraped from commercial platforms will be removed.
