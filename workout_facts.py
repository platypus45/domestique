"""workout_facts.py — L1 FACTS layer for the watertight classifier (v3.2.0).

Every library ZWO carries objective, deterministic content facts derived from
the classifier's OWN parser (scripts/classify_library_content.py's
parse_zwo_full + extract_features_v104 — NOT a fourth parser). The planner's
slot-admission gates (training_planner.file_admissible, D3) read these facts,
so no slot can be served content outside its contract even when the file's
LABEL is imperfect.

Schema (skinny, per IP amendment A2 — net-new columns only, everything else
reuses existing classifier feature columns):
  sha1      content sha1 of the .zwo bytes (cache key part)
  dur_s     content_duration_s — total INCLUDING FreeRide seconds (A1; the
            index imputes FreeRide as Z2@65%, so index Duration includes it)
  fr_s      FreeRide seconds (dur_s - valid seconds) — lets P1 index-parity
            tests exclude FreeRide files for IF/TSS (A1)
  tss       content_tss over non-FreeRide seconds (classifier semantics)
  if        content_if — the classifier's if_fraction (RMS, excl FreeRide; A1)
  hi_s      z5+z6+z7 seconds (existing z_seconds feature; D3 vo2max row)
  t130/l130 total / longest contiguous run (s) at >=1.30 FTP
  t150/l150 same at >=1.50
  t200/l200 same at >=2.00
  t240/l240 same at >=2.40
  t101/l101 same at >=1.01 — R4/R5 (2026-07-07): the SS/tempo sustained
            supra-FTP ceiling (D3 row gains `l101 < 300`). The incident class
            (3x16min @1.03 FTP served on a SWEET SPOT slot) registers in NO
            pre-v2 field: 1.03 < 1.30 so every t/l floor is blind, the
            classifier files it z4/threshold, and its IF (0.806) sits BELOW
            many legit SS files. 1.01 = strictly-above-FTP semantics (a
            1.00-exact block stays admissible per the 0.95-1.00 steady-work
            spec); grill P3: floors 1.005 vs 1.01 change ZERO verdicts at
            the 300s run length across the whole library.
  n130_45   count of contiguous runs >=45s at >=1.30 (z2 D3 row)
  sprints   the classifier's sprint_segment_count (>=1.50, 5-30s reps)
Unparseable files get {"sha1": ..., "null": true} — the ONLY fail-closed
class (A5): file_admissible treats a null row as inadmissible everywhere.

Cache: workouts/.workout_facts.json keyed by (filename, content sha1).
Incremental (only new/changed hashes recomputed), byte-deterministic
(sorted keys, fixed separators — pinned by test), never touches the other
two caches. Build hooks the index self-heal path in
training_planner.load_workout_library so a new/changed file gets facts
inline (~9ms) at the same moment the row index heals (A5).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("workout_facts")

FACTS_FILENAME = ".workout_facts.json"
# R4/R5 (2026-07-07): v2 adds t101/l101 (sustained supra-FTP run metrics for
# the SS/tempo D3 ceiling). Version mismatch drops the WHOLE cache file
# (_read_cache_file), so a v1 cache can never leak rows missing the new keys
# into file_admissible — the committed cache is rebuilt offline and shipped.
_SCHEMA_VERSION = 2

# In-process cache: {str(workout_dir): {fname: row}} — mirrors the planner's
# _CONTENT_CLASSIFICATION_CACHE pattern (load once, write-through on heal).
_FACTS_CACHE: dict[str, dict] = {}
_LOCK = threading.RLock()

_CLC = None  # lazily imported scripts/classify_library_content.py module


def _clc():
    """Import the classifier script module once (reuses ITS parser/features)."""
    global _CLC
    if _CLC is None:
        script = Path(__file__).resolve().parent / "scripts" / "classify_library_content.py"
        spec = importlib.util.spec_from_file_location("_wf_clc", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CLC = mod
    return _CLC


def reset_cache() -> None:
    """Drop the in-process cache (tests / after external cache edits)."""
    with _LOCK:
        _FACTS_CACHE.clear()


def _runs_at(power: list[float], floor: float) -> list[int]:
    """Lengths (s) of contiguous runs at >= floor FTP. FreeRide sentinel
    seconds (< 0) break runs — free seconds can never count as supra work."""
    out: list[int] = []
    run = 0
    for p in power:
        if p >= floor:
            run += 1
        else:
            if run:
                out.append(run)
            run = 0
    if run:
        out.append(run)
    return out


def compute_facts_row(zwo_path: Path, sha1: str | None = None) -> dict:
    """Facts row for one file. Unparseable → {"sha1":…, "null": true} (A5)."""
    if sha1 is None:
        sha1 = hashlib.sha1(zwo_path.read_bytes()).hexdigest()
    try:
        clc = _clc()
        power, tags, meta, segments = clc.parse_zwo_full(zwo_path)
        if not power:
            return {"sha1": sha1, "null": True}
        feats = clc.extract_features_v104(power, segments)
    except Exception as e:  # noqa: BLE001 — any parse failure = null row
        log.debug("facts parse failed for %s: %s", zwo_path.name, e)
        return {"sha1": sha1, "null": True}
    valid = [p for p in power if p >= 0]
    z = feats["z_seconds"]
    # R4/R5 (2026-07-07): 1.01 floor for the SS/tempo sustained-supra ceiling
    # (schema v2). Same _runs_at primitive as the burst floors — FreeRide
    # sentinel seconds still break runs.
    r101 = _runs_at(power, 1.01)
    r130 = _runs_at(power, 1.30)
    r150 = _runs_at(power, 1.50)
    r200 = _runs_at(power, 2.00)
    r240 = _runs_at(power, 2.40)
    return {
        "sha1": sha1,
        "dur_s": len(power),                          # A1: INCL FreeRide
        "fr_s": len(power) - len(valid),
        "tss": round(sum(p * p for p in valid) / 3600.0 * 100.0, 1),
        "if": feats["if_fraction"],                   # A1: classifier RMS
        "hi_s": z["z5"] + z["z6"] + z["z7"],
        "t101": sum(r101), "l101": max(r101, default=0),
        "t130": sum(r130), "l130": max(r130, default=0),
        "t150": sum(r150), "l150": max(r150, default=0),
        "t200": sum(r200), "l200": max(r200, default=0),
        "t240": sum(r240), "l240": max(r240, default=0),
        "n130_45": sum(1 for x in r130 if x >= 45),
        "sprints": feats["sprint_segment_count"],
    }


def _cache_path(workout_dir: Path) -> Path:
    return Path(workout_dir) / FACTS_FILENAME


def _serialize(facts: dict) -> str:
    """Byte-deterministic serialization (pinned by test)."""
    payload = {"version": _SCHEMA_VERSION, "facts": facts}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _read_cache_file(workout_dir: Path) -> dict:
    path = _cache_path(workout_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _SCHEMA_VERSION:
            return {}
        return payload.get("facts", {}) or {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.warning("facts cache load failed (%s) — rebuilding lazily", e)
        return {}


def _write_cache_file(workout_dir: Path, facts: dict) -> None:
    """Atomic best-effort write (read-only dir just means no persistence)."""
    path = _cache_path(workout_dir)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(_serialize(facts), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("facts cache write skipped (%s)", e)


def load_facts(workout_dir: Path) -> dict:
    """The {fname: row} map for this dir (in-process cached; no builds)."""
    key = str(workout_dir)
    with _LOCK:
        cached = _FACTS_CACHE.get(key)
        if cached is None:
            cached = _read_cache_file(Path(workout_dir))
            _FACTS_CACHE[key] = cached
        return cached


def ensure_facts(workout_dir: Path, zwo_paths: list[Path] | None = None) -> dict:
    """Incremental build: compute facts for new/changed files, prune deleted.

    Called from training_planner.load_workout_library's index self-heal path
    (A5) and from the offline builder. Only missing/changed (filename, sha1)
    pairs are recomputed; the cache file is rewritten only when something
    changed. Returns the up-to-date {fname: row} map.
    """
    workout_dir = Path(workout_dir)
    if zwo_paths is None:
        zwo_paths = sorted(workout_dir.glob("*.zwo"))
    with _LOCK:
        facts = dict(load_facts(workout_dir))
        dirty = False
        names = set()
        for p in zwo_paths:
            names.add(p.name)
            try:
                sha1 = hashlib.sha1(p.read_bytes()).hexdigest()
            except OSError:
                continue
            row = facts.get(p.name)
            if row is not None and row.get("sha1") == sha1:
                continue
            facts[p.name] = compute_facts_row(p, sha1=sha1)
            dirty = True
        stale = [fn for fn in facts if fn not in names]
        for fn in stale:
            del facts[fn]
            dirty = True
        if dirty:
            _write_cache_file(workout_dir, facts)
        _FACTS_CACHE[str(workout_dir)] = facts
        return facts


def get_facts(workout_dir: Path, fname: str) -> dict | None:
    """Row for one file, self-healing a missing entry inline (~9ms).

    Returns None when the file has no facts and cannot be healed (file gone).
    A returned row with {"null": true} means unparseable → caller treats as
    inadmissible (A5 — the only fail-closed class).
    """
    facts = load_facts(workout_dir)
    row = facts.get(fname)
    if row is not None:
        return row
    path = Path(workout_dir) / fname
    if not path.exists():
        return None
    with _LOCK:
        facts = load_facts(workout_dir)
        row = facts.get(fname)
        if row is not None:
            return row
        row = compute_facts_row(path)
        updated = dict(facts)
        updated[fname] = row
        _write_cache_file(workout_dir, updated)
        _FACTS_CACHE[str(workout_dir)] = updated
        return row


# ── D2 label-vs-facts contradiction audit (A3: easy-label-hides-hard ONLY) ──
#
# Rule table, locked by the grill: an EASY label may not hide hard content.
#   recovery              : any work at >=1.30 (t130 > 0 is the D3 slot rule;
#                           the LABEL rule flags sustained/burst supra only)
#   endurance(_intervals) : rep >=45s @>=1.30, or t200 > 10s, or IF > 0.80
#   tempo*/sweet_spot*    : any t200, or a >=45s run at >=1.50
#   threshold*(label)     : any t200 (ceiling 120s for the ledger-verified
#                           fused bodies, exempt BY NAME — A3/A4)
# Hard-class dose MINIMUMS live in the D3 slot contracts, not here (A3).
_EASY_LABELS_STRICT = ("recovery",)
_EASY_LABELS_Z2 = ("endurance", "endurance_intervals")
_EASY_LABELS_MID = ("tempo", "tempo_intervals", "tempo_ladder",
                    "sweet_spot", "sweet_spot_ladder")
_THRESHOLD_LABELS = ("threshold", "threshold_ladder")
_THRESHOLD_EXEMPT_T200_CEILING_S = 120


def label_contradictions(label: str, row: dict,
                         threshold_exempt: frozenset[str] = frozenset(),
                         fname: str = "") -> list[str]:
    """Reasons the (label, facts) pair is an easy-hides-hard contradiction.

    Empty list = coherent. ``threshold_exempt`` pins the ledger-verified
    fused-threshold bodies by name (A4); they stay exempt only while their
    t200 stays within the 120s ceiling.
    """
    if not row or row.get("null"):
        return []
    out: list[str] = []
    if label in _EASY_LABELS_STRICT:
        if row["n130_45"] > 0:
            out.append("rep>=45s@130")
        if row["t200"] > 0:
            out.append("t200>0")
        if row["if"] > 0.75:
            out.append("if>0.75")
    elif label in _EASY_LABELS_Z2:
        if row["n130_45"] > 0:
            out.append("rep>=45s@130")
        if row["t200"] > 10:
            out.append("t200>10")
        if row["if"] > 0.80:
            out.append("if>0.80")
    elif label in _EASY_LABELS_MID:
        if row["t200"] > 0:
            out.append("t200>0")
        if row["l150"] >= 45:
            out.append("run>=45s@150")
    elif label in _THRESHOLD_LABELS:
        if row["t200"] > 0:
            if fname in threshold_exempt and row["t200"] <= _THRESHOLD_EXEMPT_T200_CEILING_S:
                pass  # ledger-verified fused body within the 120s ceiling
            else:
                out.append("t200>0")
    return out


def audit_labels(facts: dict, classifications: dict,
                 threshold_exempt: frozenset[str] = frozenset(),
                 ftp_test_names: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """Full-library D2 sweep → {fname: [reasons]} for every contradiction.

    ``classifications`` is the .content_classification.json
    ``classifications`` map. ftp_test-labeled/tagged files are exempt
    (protocol tests are maximal by design).
    """
    flagged: dict[str, list[str]] = {}
    for fname, row in facts.items():
        entry = classifications.get(fname) or {}
        label = entry.get("primary") or ""
        if not label or label == "ftp_test" or fname in ftp_test_names:
            continue
        reasons = label_contradictions(label, row,
                                       threshold_exempt=threshold_exempt,
                                       fname=fname)
        if reasons:
            flagged[fname] = reasons
    return flagged
