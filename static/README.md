# static/

Static assets served by FastAPI. `app.py` mounts the folder at
`/static`:

```python
app.mount("/static", StaticFiles(directory="static"))
```

The whole folder ships into the PyInstaller bundle via
`("static","static")` in `domestique.spec` `datas=`.

## Layout

| Path | Purpose |
|---|---|
| `favicon.png` | Browser tab icon, referenced by every template. |
| `icon.png` | App icon (synced from `assets/icon.png`). |
| `apple-touch-icon.png` | iOS home-screen icon — `dashboard.html` adds it. |
| `icon.svg` | Vector icon source. |
| `js/smooth-display.js` | Scroll/animation helper. Loaded explicitly by templates that need it. |
| `vendor/html2canvas.min.js` | Bundled third-party library for client-side PNG export of the finished-programme summary. |

## Path case sensitivity

File paths are **case-sensitive on Linux** even when macOS / Windows
default to case-insensitive filesystems. If you reference
`/static/icon.png` from a template, the on-disk file must be exactly
`icon.png` — `Icon.PNG` will silently 404 on a Linux user's machine.
