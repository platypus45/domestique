"""v3.11.6 — the workout classification index warning, made true for riders.

The old check hashed (filename, mtime) of every .zwo and compared it with the
hash stored at classification time. No install pipeline preserves mtimes, so
every packaged install mismatched forever: three warnings per start (the
owner's log: 54 in three weeks; a rider's diagnostics in issue #11) telling
riders to run `scripts/classify_library_content.py`, which the app does not
ship. The planner used the index regardless — the warning was noise.

Now: a warning only when a workout in the folder has no entry (then it really
is classified by name), once per process; the developer hint only from a
source checkout, where the mtime hash still flags an in-place edit.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402


def _folder(tmp_path: Path, files=("a.zwo", "b.zwo"), entries=None, index=True):
    wd = tmp_path / "workouts"
    wd.mkdir()
    for f in files:
        (wd / f).write_text("<workout_file/>")
    if index:
        entries = list(files) if entries is None else entries
        (wd / ".content_classification.json").write_text(json.dumps({
            "version": "v1.0.4",
            "workouts_dir_hash": "not-the-hash-of-this-folder",
            "count": len(entries),
            "classifications": {f: {"primary": "endurance"} for f in entries},
        }))
    return wd


@pytest.fixture
def loader(tmp_path, monkeypatch, caplog):
    """Point the planner at a temp folder, reset the once-guards, capture."""
    def _make(frozen: bool, **kw):
        wd = _folder(tmp_path, **kw)
        monkeypatch.setattr(tp, "WORKOUT_DIR", wd)
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        tp._CONTENT_CLASSIFICATION_CACHE = None
        tp._CLASSIFICATION_WARNED.clear()
        caplog.set_level(logging.WARNING, logger=tp.log.name)
        return wd
    yield _make
    tp._CONTENT_CLASSIFICATION_CACHE = None
    tp._CLASSIFICATION_WARNED.clear()


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == tp.log.name]


def test_packaged_install_with_full_index_is_silent(loader, caplog):
    """The bundled library: every file has an entry, only the mtimes differ."""
    loader(frozen=True)
    assert tp._load_content_classifications().keys() == {"a.zwo", "b.zwo"}
    assert _warnings(caplog) == []


def test_source_checkout_keeps_the_developer_hint_on_a_stale_hash(loader, caplog):
    loader(frozen=False)
    tp._load_content_classifications()
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "classify_library_content" in msgs[0]
    assert "stale" in msgs[0]


def test_missing_entry_warns_once_in_plain_words(loader, caplog):
    loader(frozen=True, entries=["a.zwo"])
    tp._load_content_classifications()
    # a second first-load (another thread at boot, a profile switch) is silent
    tp._CONTENT_CLASSIFICATION_CACHE = None
    tp._load_content_classifications()
    msgs = _warnings(caplog)
    assert len(msgs) == 1, msgs
    assert msgs[0].startswith("1 workout(s) in the library have no content classification")
    assert "b.zwo" in msgs[0]
    assert "classify_library_content" not in msgs[0]
    assert "scripts/" not in msgs[0]


def test_missing_entry_from_a_checkout_adds_the_hint(loader, caplog):
    loader(frozen=False, entries=["a.zwo"])
    tp._load_content_classifications()
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "classify_library_content" in msgs[0]


def test_no_index_file_speaks_to_the_rider(loader, caplog):
    """A rider's own workout folder never had an index; the planner still works."""
    loader(frozen=True, index=False)
    assert tp._load_content_classifications() == {}
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "classified by name" in msgs[0]
    assert "classify_library_content" not in msgs[0]
