"""v1.6.4 — ZWO download endpoints force attachment + octet-stream.

WKWebView (the engine used by the packaged macOS DMG) ignores
``Content-Disposition: attachment`` when the server returns
``Content-Type: application/xml`` — the file renders inline as styled
XML, and the user sees a "white screen of Times New Roman" instead of
a save dialog.

v1.6.4 changes all three ZWO endpoints to:
  - ``media_type=application/octet-stream`` so the browser always
    treats the response as a binary download.
  - explicit ``Content-Disposition: attachment; filename="..."`` header
    (was relying on FastAPI's auto-set behavior, which some webviews
    appear to honor inconsistently).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


def _pick_workout_filename() -> str:
    """Find an actual ZWO from the bundled library to probe."""
    wdir: Path = app_module.WORKOUT_DIR
    files = sorted(wdir.glob("*.zwo"))
    assert files, f"no ZWO files in WORKOUT_DIR ({wdir})"
    return files[0].name


def test_download_zwo_flat_returns_octet_attachment():
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    r = client.get(f"/api/download/zwo/{filename}")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    disp = r.headers.get("content-disposition", "")
    assert "attachment" in disp.lower()
    assert filename in disp
    # Body is still the raw ZWO XML — only the wire framing changed.
    assert r.content.startswith(b"<?xml") or b"<workout_file>" in r.content


def test_download_zwo_category_returns_octet_attachment():
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    # `/api/download/zwo/<cat>/<file>` falls back to the flat layout when
    # the category subdir is missing — that's the path the dashboard's
    # library tile hits.
    r = client.get(f"/api/download/zwo/anaerobic/{filename}")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in r.headers.get("content-disposition", "").lower()


def test_workout_download_returns_octet_attachment():
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    r = client.get(f"/api/workout/download/{filename}")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    disp = r.headers.get("content-disposition", "")
    assert "attachment" in disp.lower()
    assert filename in disp


def test_download_zwo_404_for_unknown_file():
    """Regression: 404 path is unchanged by the header fix."""
    client = TestClient(app_module.app)
    r = client.get("/api/download/zwo/__does_not_exist__.zwo")
    assert r.status_code == 404
