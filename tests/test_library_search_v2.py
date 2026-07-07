"""Library search pass 2 (R2, 2026-07-07) — cyclist-query regression suite.

Locks the tokenized /api/workouts?search= grammar:
  * multi-token AND over the normalized haystack ("threshold 3x16");
  * duration intents ("90min"/"1h30"/"1.5h" = ±12%; "<60"/">120"/"60-90"
    literal) intersected with the slider window;
  * structure tokens ("3x16", and "30/15" ≡ "30-15" ≡ "30s15s");
  * intensity ("@105" = ±2 pts vs the "@ 91%" embedded in display names);
  * synonyms + ø→o ("ss", "ronnestad"/"rønnestad", "ou", …);
  * bounded Levenshtein-1 typo rescue over the class vocabulary ONLY;
  * relevance ranking (sort=relevance) with exact-name-first;
  * pass-1 compatibility (family counts, no 500s on garbage);
  * the frontend highlighter's escape-safety + the "/" hotkey guards
    (node harness over the extracted dashboard.html function).

Uses the real WORKOUT_DIR via TestClient — same parse path the dashboard
hits, same hermeticity model as tests/test_library_filters.py.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402

client = TestClient(app_module.app)

DASHBOARD = ROOT / "templates" / "dashboard.html"


def _get(**params):
    r = client.get("/api/workouts", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    return r, body


def _files(rows) -> set[str]:
    return {w["File"] for w in rows}


# ── tokenized AND semantics ──────────────────────────────────────────────────


def test_threshold_3x16_and_semantics():
    """"threshold 3x16" → nonzero; every row carries the 3x16 structure."""
    _r, rows = _get(search="threshold 3x16", limit=6000)
    assert rows, "expected at least one Threshold 3x16 workout"
    rx = re.compile(r"(?<!\d)3x16(?!\d)")
    for w in rows:
        hay = app_module._search_row_haystack(w)
        assert rx.search(hay), f"{w['File']} lacks 3x16"
        assert (w.get("content_class") or "").startswith("threshold")


def test_structure_slash_hyphen_compact_equivalent():
    """"30/15" ≡ "30-15" ≡ "30s15s" — identical result sets, nonzero."""
    sets = [_files(_get(search=q, limit=6000)[1]) for q in ("30/15", "30-15", "30s15s")]
    assert sets[0], "library should have 30s15s workouts"
    assert sets[0] == sets[1] == sets[2]


# ── typo rescue ──────────────────────────────────────────────────────────────


def test_typo_treshold_equals_threshold():
    """"treshold" (Levenshtein 1 from class vocab) ≡ "threshold"."""
    a = _files(_get(search="treshold", limit=6000)[1])
    b = _files(_get(search="threshold", limit=6000)[1])
    assert a == b and a


def test_typo_rescue_does_not_fuzz_real_tokens():
    """A token that DOES hit the haystack is never re-mapped ("tempo9x…" no,
    plain "tempo" stays the tempo family, not a fuzz candidate)."""
    _r, rows = _get(search="tempo", limit=6000)
    assert rows
    for w in rows:
        assert (w.get("content_class") or "").startswith("tempo")


# ── duration intents ─────────────────────────────────────────────────────────


def test_ss_90min_family_plus_duration_window():
    """"ss 90min" → sweet-spot family within 90 ±12% (⊂ [79, 101])."""
    _r, rows = _get(search="ss 90min", limit=6000)
    assert rows, "expected sweet-spot workouts near 90min"
    for w in rows:
        assert (w.get("content_class") or "").startswith("sweet_spot"), w["File"]
        assert 79 <= float(w["Duration(min)"]) <= 101, w["File"]


def test_gt120_vo2_family():
    """">120 vo2" → vo2* family strictly longer than 120min."""
    _r, rows = _get(search=">120 vo2", limit=6000)
    for w in rows:
        assert (w.get("content_class") or "").startswith("vo2"), w["File"]
        assert float(w["Duration(min)"]) > 120, w["File"]
    # The library carries ≥1 long vo2 file (150min) — guard against silent 0.
    assert rows, "expected at least one vo2 workout >120min"


def test_duration_intent_intersects_slider():
    """Intent (90 ±12%) ∩ slider (max 85) → [79.2, 85], not [79.2, 100.8]."""
    _r, rows = _get(search="90min endurance", duration_max=85, limit=6000)
    assert rows
    for w in rows:
        assert 79.2 <= float(w["Duration(min)"]) <= 85, w["File"]


def test_parse_query_duration_grammar():
    """Unit lock on the parser: bare forms ±12%, comparators/ranges literal."""
    pq = app_module._search_parse_query
    for q in ("90min", "90m", "1h30", "1.5h"):
        lo, hi = pq(q)["duration"]
        assert abs(lo - 79.2) < 1e-6 and abs(hi - 100.8) < 1e-6, q
    assert pq("<60")["duration"][1] < 60
    lo, hi = pq(">120")["duration"]
    assert lo > 120 and hi == float("inf")
    assert pq("60-90")["duration"] == (60.0, 90.0)
    # descending hyphen pair is a STRUCTURE, not a range
    p = pq("30-15")
    assert p["duration"] is None and p["structures"] == ["30s15s"]
    # slash is never a range
    assert pq("30/30")["structures"] == ["30s30s"]
    assert pq("40/20")["structures"] == ["40s20s"]
    # NxM with spaces and unit
    assert pq("3 x 16")["structures"] == ["3x16"]
    assert pq("3x16min")["structures"] == ["3x16"]
    assert pq("13x30")["structures"] == ["13x30"]


def test_parse_query_intensity_and_synonyms():
    pq = app_module._search_parse_query
    for q in ("105%", "@105", "@ 105%"):
        assert pq(q)["percent"] == 105, q
    # bigram synonym + family values
    assert pq("sweet spot")["families"] == [("sweet_spot", "sweet spot")]
    assert pq("ou")["families"] == [("over_under", "ou")]
    assert pq("v02")["families"] == [("vo2", "v02")]
    # ø→o transliteration
    assert pq("rønnestad")["semantics"] == [("ronnestad", "", "ronnestad")]


# ── synonyms / semantic tokens against the live library ─────────────────────


def test_ronnestad_superset_of_tagged_files():
    """"ronnestad" ⊇ every is_ronnestad-tagged file (incl. the new 30/15 trio),
    and ø-spelling is identical."""
    _r, rows = _get(search="ronnestad", limit=6000)
    assert rows, "ronnestad search must not be empty"
    got = _files(rows)
    cache = ROOT / "workouts" / ".content_classification.json"
    if cache.exists():
        cls = json.loads(cache.read_text()).get("classifications", {})
        tagged = {fn for fn, e in cls.items() if "is_ronnestad" in (e.get("tags") or [])}
        in_library = tagged & _files(_get(limit=6000)[1])
        assert in_library, "expected is_ronnestad-tagged files in the library"
        missing = in_library - got
        assert not missing, f"ronnestad search missed tagged files: {missing}"
    assert got == _files(_get(search="rønnestad", limit=6000)[1])


def test_intensity_at105_matches_embedded_percents():
    """"@105" → rows whose names embed a percent within ±2 of 105."""
    _r, rows = _get(search="@105", limit=6000)
    if not rows:
        pytest.skip("library has no @103-107% workouts")
    rx = app_module._SEARCH_HAY_PCT_RX
    for w in rows[:80]:
        hay = app_module._search_row_haystack(w)
        pcts = [int(g) for tup in rx.findall(hay) for g in tup if g]
        pcts = [p for p in pcts if 40 <= p <= 200]
        if pcts:
            assert any(abs(p - 105) <= 2 for p in pcts), (w["File"], pcts)
        else:
            assert "105" in hay, w["File"]


# ── pass-1 compatibility ─────────────────────────────────────────────────────


def test_threshold_family_count_pass1_compatible():
    """search=threshold ⊇ pass-1's 932-row class family (== or superset)."""
    _r, rows = _get(search="threshold", limit=6000)
    assert len(rows) >= 932
    for w in rows:
        assert (w.get("content_class") or "").startswith("threshold")


def test_family_token_with_type_filter_stays_substring():
    """Pass-1 guard: Type=threshold + search=vo2 must NOT hijack to the vo2
    class (which would guarantee 0 rows) — it intersects as a substring."""
    _r, rows = _get(search="vo2", content_class="threshold", limit=6000)
    for w in rows:
        assert (w.get("content_class") or "") == "threshold"
        assert "vo2" in app_module._search_row_haystack(w)


def test_garbage_queries_do_not_500():
    for q in ("", "   ", "@@@@ <<>> !!", "zzzzzzzzzz", "<", ">", "x", "///",
              "99999x99999", "0-0", "@999%", "a" * 500):
        r = client.get("/api/workouts", params={"search": q, "limit": 50})
        assert r.status_code == 200, (q, r.status_code)


# ── relevance ranking ────────────────────────────────────────────────────────


def test_relevance_exact_name_query_ranks_first():
    """Querying a row's exact Name with sort=relevance puts that Name first."""
    _r, probe = _get(search="3x16", limit=50)
    assert probe
    target = next((w for w in probe if w["File"] == "threshold_3x16min_118min.zwo"),
                  probe[0])
    _r, ranked = _get(search=target["Name"], sort="relevance", limit=6000)
    assert ranked
    assert ranked[0]["Name"] == target["Name"]


def test_relevance_without_search_falls_back_to_duration():
    _r, rows = _get(sort="relevance", limit=100)
    durs = [float(w["Duration(min)"]) for w in rows]
    assert durs == sorted(durs)


def test_relevance_class_query_prefers_name_hits():
    """Rows whose NAME carries the token outrank class-only matches."""
    _r, rows = _get(search="threshold", sort="relevance", limit=6000)
    assert rows
    first_hay = (rows[0].get("display_name") or rows[0]["Name"]).lower()
    assert "threshold" in first_hay


# ── intent echo headers ──────────────────────────────────────────────────────


def test_intent_headers_echo_parsed_query():
    r, _rows = _get(search="threshold 3x16 90min", limit=10)
    intents = json.loads(r.headers["X-Search-Intents"])
    assert "threshold" in intents and "3x16" in intents and "~90min" in intents
    tokens = json.loads(r.headers["X-Search-Tokens"])
    assert "threshold" in tokens and "3x16" in tokens


def test_no_intent_headers_without_search():
    r = client.get("/api/workouts", params={"limit": 5})
    assert "X-Search-Intents" not in r.headers
    assert "X-Search-Tokens" not in r.headers


# ── parser helpers ───────────────────────────────────────────────────────────


def test_lev1_helper():
    lev = app_module._search_lev1
    assert lev("treshold", "threshold")      # one insertion
    assert lev("thresholt", "threshold")     # one substitution
    assert lev("threshold", "threshold")     # equal
    assert not lev("thrshld", "threshold")   # distance 2
    assert not lev("tempo", "temps5")        # length gap 1 + substitution = 2


# ── frontend: highlighter escape-safety + "/" hotkey guard (node harness) ────


def _extract_js_function(src: str, name: str) -> str:
    """Slice `function <name>(...) {...}` out of dashboard.html by brace count."""
    start = src.index(f"function {name}")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_highlight_is_escape_safe():
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "_libHighlightName")
    harness = """
const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
let _libSearchTokens = [];
%s
// 1) token "amp" over a name with "&" must not corrupt the entity
_libSearchTokens = ['amp'];
let out = _libHighlightName('R&B ramp 60min');
if (out.includes('&<mark>amp</mark>;')) throw new Error('entity corrupted: ' + out);
if (!out.includes('&amp;')) throw new Error('& not escaped: ' + out);
if (!out.includes('r<mark>amp</mark>')) throw new Error('match lost: ' + out);
// 2) hostile name is fully escaped
_libSearchTokens = ['script'];
out = _libHighlightName('<script>alert(1)</script>');
if (out.includes('<script')) throw new Error('unescaped tag: ' + out);
if (!out.includes('<mark>script</mark>')) throw new Error('no mark: ' + out);
// 3) structure token matches the pretty display form (3×16)
_libSearchTokens = ['3x16'];
out = _libHighlightName('Threshold 118min — 3×16min @ 91%%');
if (!out.includes('<mark>3×16</mark>')) throw new Error('display-form miss: ' + out);
// 4) regex metacharacters in a token must not throw
_libSearchTokens = ['a+b(', '**'];
out = _libHighlightName('a+b( zone **test');
if (!out.includes('<mark>a+b(</mark>')) throw new Error('meta token miss: ' + out);
// 5) no tokens → plain escaped passthrough
_libSearchTokens = [];
out = _libHighlightName('A & B');
if (out !== 'A &amp; B') throw new Error('passthrough broken: ' + out);
console.log('OK');
""" % fn
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout


def test_slash_hotkey_guards_present():
    """The "/" handler must gate on: library tab active, no other input
    focused, no modal open — and Esc-clear must live on the search box."""
    src = DASHBOARD.read_text(encoding="utf-8")
    i = src.index("e.key !== '/'")
    handler = src[i:i + 700]
    assert "sec-library" in handler and "classList.contains('active')" in handler
    assert "'input'" in handler and "'textarea'" in handler and "'select'" in handler
    assert "isContentEditable" in handler
    assert "#modal-overlay.open" in handler
    assert "preventDefault" in handler
    assert 'onkeydown="onLibSearchKeydown(event)"' in src
    assert "onLibSearchKeydown" in src and "e.key !== 'Escape'" in src
