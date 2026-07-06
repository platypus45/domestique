"""AC1 chip (post-v3.2.2) — power-curve backfill respects the sync-write gate.

power_curve.backfill_icu_history used to write every hydrated envelope into
the LIVE active-profile rides dir with no db.sync_write_gate: a profile
switch mid-backfill mis-filed the remaining envelopes into the new profile.
Now the caller's db.snapshot_sync_identity() snapshot is threaded through as
``sync_snapshot`` and each per-ride write runs inside the gate — an identity
change (switch/purge/epoch bump) raises SyncAborted on the next write and
the loop stops cleanly with status "aborted".

The flip is simulated by bumping db._sync_epoch (the same identity axis a
profile switch invalidates) from the patched streams fetcher — i.e. exactly
"mid-loop, between fetch and write".
"""
import json

import pytest

import db
import power_curve


def _mk_envelope(dirpath, name, ext_id):
    p = dirpath / name
    p.write_text(json.dumps({
        "id": ext_id, "external_id": ext_id,
        "started_at": "2026-07-01T10:00:00", "streams": {},
    }), encoding="utf-8")
    return p


@pytest.fixture
def rides_dir(tmp_path, monkeypatch):
    d = tmp_path / "icu_rides"
    d.mkdir()
    monkeypatch.setattr(power_curve, "_icu_rides_dir", lambda: d)
    # Hermetic single-flight lock under tmp
    monkeypatch.setattr(power_curve, "_backfill_lock_path",
                        lambda: tmp_path / ".backfill.lock")
    # Every envelope needs hydration; efforts derivation always succeeds.
    monkeypatch.setattr(power_curve, "_needs_refetch", lambda p: True)
    monkeypatch.setattr(power_curve, "_extract_efforts_from_streams",
                        lambda s: [{"secs": 5, "watts": 500}])
    return d


def test_profile_flip_mid_backfill_stops_writes(rides_dir, monkeypatch):
    for i in range(3):
        _mk_envelope(rides_dir, f"ride_{i}.json", f"i{i}")

    calls = {"n": 0}

    def fake_streams(ext):
        calls["n"] += 1
        if calls["n"] == 2:
            # Identity flips between fetch #2 and its write — the gate must
            # refuse that write and every one after it.
            db._sync_epoch += 1
        return {"watts": [200, 500], "time": [0, 1]}

    import training
    monkeypatch.setattr(training, "fetch_activity_streams", fake_streams)

    snap = db.snapshot_sync_identity()
    result = power_curve.backfill_icu_history(sync_snapshot=snap)

    assert result["status"] == "aborted"
    assert result["backfilled"] == 1  # ride 1 landed pre-flip, nothing after
    hydrated = [p.name for p in sorted(rides_dir.glob("*.json"))
                if json.loads(p.read_text(encoding="utf-8")).get("efforts")]
    assert hydrated == ["ride_0.json"], hydrated


def test_no_flip_backfills_everything_gated(rides_dir, monkeypatch):
    for i in range(3):
        _mk_envelope(rides_dir, f"ride_{i}.json", f"i{i}")
    import training
    monkeypatch.setattr(training, "fetch_activity_streams",
                        lambda ext: {"watts": [200, 500], "time": [0, 1]})

    result = power_curve.backfill_icu_history(
        sync_snapshot=db.snapshot_sync_identity())

    assert result["status"] == "ok"
    assert result["backfilled"] == 3


def test_no_snapshot_keeps_legacy_ungated_path(rides_dir, monkeypatch):
    _mk_envelope(rides_dir, "ride_0.json", "i0")
    import training
    monkeypatch.setattr(training, "fetch_activity_streams",
                        lambda ext: {"watts": [200, 500], "time": [0, 1]})

    result = power_curve.backfill_icu_history()  # sync_snapshot=None

    assert result["status"] == "ok"
    assert result["backfilled"] == 1
