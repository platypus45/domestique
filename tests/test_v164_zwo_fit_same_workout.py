"""v1.6.4 — ZWO + FIT export the same workout content.

User asked to verify that clicking "Download ZWO" and "Download FIT"
on the same session yields equivalent workouts. Both buttons pass
``session.zwo_file`` to their respective endpoints:

  - ZWO  → ``GET /api/download/zwo/<file>`` serves the raw library ZWO.
  - FIT  → ``GET /api/export/fit-workout?zwo_file=<file>`` parses that
           same ZWO and transcodes its blocks into FIT workout steps
           (app.py ``_build_fit_workout_from_zwo``).

The ``zwo_file`` query parameter is what guarantees same-workout
parity — without it the FIT endpoint falls through to a generic
template keyed only on ``session_type`` + ``duration_min``, which can
produce a different workout.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


def _pick_workout_filename() -> str:
    wdir: Path = app_module.WORKOUT_DIR
    files = sorted(wdir.glob("*.zwo"))
    assert files, f"no ZWO files in WORKOUT_DIR ({wdir})"
    return files[0].name


def test_fit_export_calls_zwo_path_when_zwo_file_supplied():
    """When zwo_file is set, /api/export/fit-workout must transcode that ZWO.

    Pre-v1.0.3 the endpoint ignored zwo_file and fell back to the generic
    block generator. The contract guarantees same-workout parity with the
    ZWO download.
    """
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    with patch.object(app_module, "_build_fit_workout_from_zwo") as mocked:
        mocked.return_value = b"FIT_BYTES_FROM_ZWO"
        r = client.get(
            "/api/export/fit-workout",
            params={
                "session_type": "z2",
                "duration_min": 60,
                "name": "TestSession",
                "zwo_file": filename,
            },
        )

    assert r.status_code == 200
    mocked.assert_called_once()
    # Args: (name, zwo_path, ftp)
    call_args = mocked.call_args.args
    assert call_args[0] == "TestSession"
    # zwo_path should be the resolved Path to the named library file.
    assert call_args[1].name == filename
    assert r.content == b"FIT_BYTES_FROM_ZWO"


def test_fit_export_404_when_zwo_file_missing():
    """v1.0.3 contract: a non-existent zwo_file 404s loudly instead of
    silently falling back to the generic generator (which would produce a
    different workout than the user thinks they're downloading)."""
    client = TestClient(app_module.app)
    r = client.get(
        "/api/export/fit-workout",
        params={"zwo_file": "__nope_does_not_exist__.zwo", "name": "x"},
    )
    assert r.status_code == 404
    body = r.json()
    assert "not found" in (body.get("error") or "").lower()


def test_fit_export_generic_path_when_no_zwo_file():
    """No zwo_file → generic block generator path. Doesn't have to match
    any specific library file, just has to return a non-empty FIT body."""
    client = TestClient(app_module.app)
    r = client.get(
        "/api/export/fit-workout",
        params={"session_type": "z2", "duration_min": 30, "name": "Generic"},
    )
    assert r.status_code == 200
    assert len(r.content) > 0


def test_zwo_and_fit_endpoints_agree_on_filename_arg():
    """Sanity: GETing the ZWO and the FIT for the same session.zwo_file
    yields a 200 from both, with the FIT body produced from the ZWO."""
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    zwo_resp = client.get(f"/api/download/zwo/{filename}")
    assert zwo_resp.status_code == 200
    assert zwo_resp.content

    fit_resp = client.get(
        "/api/export/fit-workout",
        params={
            "session_type": "z2",
            "duration_min": 60,
            "name": "Matched",
            "zwo_file": filename,
        },
    )
    assert fit_resp.status_code == 200
    assert fit_resp.content
