"""Linux-IP Part B — layout invariants + spec consistency (layers 3 & 4).

The app resolves every data dir as ``Path(module.__file__).parent / <sibling>``,
so code and data must live side by side — in the repo AND inside the frozen
bundle. These tests pin both:

  * layer 3: every sibling target the modules reference exists next to the
    modules, wherever they live (valid before and after any repo move);
  * layer 4: domestique.spec's datas SOURCES exist relative to the spec dir
    (a wrong source fails the build loudly — this fails the unit gate
    faster) and its DESTINATIONS equal the pinned set (a wrong destination
    builds fine and degrades silently — the v3.3.0 / Linux-report class).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import training_planner as tp

ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = Path(tp.__file__).resolve().parent


# ── Layer 3: sibling data targets exist next to the modules ─────────────────

_SIBLING_DIRS = ("workouts", "courses", "templates", "static", "profiles")
_SIBLING_FILES = ("routes.json", "profiles_indexed.json",
                  "surface_types.json", "config.py")


def test_module_sibling_dirs_exist():
    for d in _SIBLING_DIRS:
        assert (CODE_DIR / d).is_dir(), f"{d}/ must sit next to the modules"


def test_module_sibling_files_exist():
    for f in _SIBLING_FILES:
        assert (CODE_DIR / f).is_file(), f"{f} must sit next to the modules"


def test_classifier_script_reachable_from_workout_facts():
    # workout_facts loads scripts/classify_library_content.py BY PATH as a
    # sibling — the 3.3.0 storm shipped without it.
    assert (CODE_DIR / "scripts" / "classify_library_content.py").is_file()


def test_workout_library_hidden_caches_present():
    for f in (".content_classification.json", ".library_index.json",
              ".workout_facts.json"):
        assert (CODE_DIR / "workouts" / f).is_file(), f


def test_version_file_resolvable():
    # app.py reads VERSION next to itself (frozen) or one level up (dev,
    # after a repo-layout move). At least one must exist.
    assert (CODE_DIR / "VERSION").is_file() \
        or (CODE_DIR.parent / "VERSION").is_file()


def test_bundled_workout_dir_is_populated():
    zwo = list(tp._BUNDLED_WORKOUT_DIR.glob("*.zwo"))
    assert len(zwo) >= 4000, \
        f"bundled library holds {len(zwo)} .zwo files — expected ≥4000"


# ── Layer 4: domestique.spec datas contract ─────────────────────────────────

# The DESTINATION set is the frozen app's data layout — module code resolves
# these as _MEIPASS-siblings, so this set changing means runtime breakage.
# Change it only together with the code that reads the moved data.
_PINNED_DESTINATIONS = {
    "templates", "courses", "workouts", "static", "assets", ".", "scripts",
}


def _spec_datas() -> list[tuple[str, str]]:
    spec = None
    for cand in (ROOT / "packaging" / "domestique.spec", ROOT / "domestique.spec"):
        if cand.is_file():
            spec = cand
            break
    assert spec is not None, "domestique.spec not found"
    text = spec.read_text(encoding="utf-8")
    m = re.search(r"^datas = \[(.*?)^\]", text, re.S | re.M)
    assert m, "could not locate the datas = [...] block in domestique.spec"
    entries = ast.literal_eval("[" + m.group(1) + "]")
    return [(str(s), str(d)) for s, d in entries], spec.parent


def test_spec_datas_sources_exist():
    entries, spec_dir = _spec_datas()
    missing = [s for s, _ in entries if not (spec_dir / s).exists()]
    assert missing == [], f"spec datas sources missing: {missing}"


def test_spec_datas_destinations_pinned():
    entries, _ = _spec_datas()
    dests = {d for _, d in entries}
    unsanctioned = dests - _PINNED_DESTINATIONS
    assert unsanctioned == set(), (
        f"spec datas DESTINATIONS changed: {unsanctioned}. A changed "
        "destination silently breaks every frozen Path(__file__).parent "
        "data read — change it only together with the reading code, then "
        "update _PINNED_DESTINATIONS deliberately.")


def test_spec_bundles_the_critical_data():
    entries, _ = _spec_datas()
    dests = {d for _, d in entries}
    for needed in ("workouts", "templates", "static", "courses"):
        assert needed in dests, f"spec no longer bundles {needed}/"
    # VERSION must land at the bundle root.
    assert any(d == "." and s.endswith("VERSION") for s, d in entries)
