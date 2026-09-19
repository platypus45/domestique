"""Library search: parse a query, match a row, score the hit.

Eight helpers and seven tables that together are the whole of what
/api/workouts does with a `q=` parameter -- normalisation and transliteration,
the synonym and typo vocabularies, the duration and percentage grammars, the
Levenshtein-1 fallback, and the ranking.

It is the most separable piece in the file: outside `re` and its own tables it
referenced nothing, and no code outside the WORKOUT APIs section called into
it. The only external readers are three tests reaching `app._search_*`, which
the re-export keeps working.

`_SEARCH_HAYS_CACHE` is module state and is NOT re-exported from app.py; it is
a one-entry memo keyed on the identity of the rows list, so a stale binding
elsewhere would memoise against a list nobody else holds.
"""
import re


# ø→o / ×→x transliteration applied to BOTH query and haystack, so
# "rønnestad" matches "ronnestad" and a typed "3x16" matches the display
# name's "3×16min". En-dash folds to "-" (then to space), em-dash to space.
_SEARCH_TRANSLIT = str.maketrans({"ø": "o", "×": "x", "–": "-", "—": " "})

def _search_normalize(s: str) -> str:
    """Shared normalizer: lowercase, transliterate, [-_/]→space, collapse ws.

    Collapsing separators is what lets one token grammar span filenames
    (threshold_2x16min-10min_98pct_118min.zwo), ZWO names (Threshold 3x16min) and
    display names (Threshold 118min — 3×16min @ 91%).
    """
    s = (s or "").lower().translate(_SEARCH_TRANSLIT)
    s = re.sub(r"[-_/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _search_row_haystack(row: dict) -> str:
    """Normalized searchable text for one cached library row.

    Name + display_name + File + Protocol + content_class + Tags. Keeping
    display_name preserves pass-1 behaviour (the UI renders display_name, so
    typing the visible title must hit); content_class + Tags let plain tokens
    reach classifier truth ("sweet" finds sweet_spot-classed rows whose
    filename says otherwise).
    """
    return _search_normalize(" ".join((
        row.get("Name") or "", row.get("display_name") or "",
        row.get("File") or "", row.get("Protocol") or "",
        row.get("content_class") or "", " ".join(row.get("Tags") or ()),
    )))

# Haystack side-cache. Keyed by IDENTITY of the cached rows list — the entry
# keeps a strong reference to that exact list, so the id can never be reused
# while the cache holds it; a library rescan swaps the rows object and the
# haystacks lazily rebuild on the next searched request (~40ms, once).
# Benign race under concurrent first-searches: both compute, one wins.
_SEARCH_HAYS_CACHE: tuple[list, list[str]] | None = None

def _get_search_haystacks(rows: list[dict]) -> list[str]:
    global _SEARCH_HAYS_CACHE
    c = _SEARCH_HAYS_CACHE
    if c is not None and c[0] is rows:
        return c[1]
    hays = [_search_row_haystack(r) for r in rows]
    _SEARCH_HAYS_CACHE = (rows, hays)
    return hays

# THE synonym map (spec: one explicit dict). token/phrase → (kind, value).
# Two-word keys are resolved by a bigram pass over the token stream.
#   family   → content_class PREFIX match (pass-1 semantics: prefix BROADENS
#              to the family — threshold → threshold + threshold_ladder —
#              and deliberately does NOT fall back to a name substring;
#              that's what fixed "sprint" matching every sprint-finish title)
#   class    → exact content_class OR the typed word as substring (union)
#   suffix   → content_class endswith OR substring (union)
#   ftp_test → ftp_test class/tag OR substring (union)
#   ronnestad/micro → special row predicates in _search_match_row
_SEARCH_SYNONYMS: dict[str, tuple[str, str]] = {
    # family aliases (absorbed from pass-1 _SEARCH_TYPE_ALIASES)
    "sprint": ("family", "neuromuscular"), "sprints": ("family", "neuromuscular"),
    "neuromuscular": ("family", "neuromuscular"),
    "recovery": ("family", "recovery"),
    "endurance": ("family", "endurance"), "z2": ("family", "endurance"),
    "tempo": ("family", "tempo"),
    "ss": ("family", "sweet_spot"), "sweetspot": ("family", "sweet_spot"),
    "sweet spot": ("family", "sweet_spot"),
    "threshold": ("family", "threshold"),
    "ou": ("family", "over_under"), "overunder": ("family", "over_under"),
    "overunders": ("family", "over_under"), "over under": ("family", "over_under"),
    "over unders": ("family", "over_under"),
    # vo2 prefix = whole family (vo2max + vo2_short + vo2_ladder); vo2_short
    # even DISPLAYS as "VO2max" in the Protocol column.
    "vo2": ("family", "vo2"), "v02": ("family", "vo2"),
    "vo2max": ("family", "vo2"), "vo2 max": ("family", "vo2"),
    "anaerobic": ("family", "anaerobic"),
    # semantic tokens
    "ronnestad": ("ronnestad", ""),          # rønnestad already ø→o folded
    "micro": ("micro", ""), "microburst": ("micro", ""), "microbursts": ("micro", ""),
    "strides": ("class", "endurance_intervals"),
    "ladder": ("suffix", "_ladder"),
    "test": ("ftp_test", ""), "ftp test": ("ftp_test", ""), "ftp_test": ("ftp_test", ""),
    # NOTE "ramp" is deliberately NOT mapped to ftp_test: every ramp-test file
    # already contains "ramp", while the union would drag in non-ramp tests
    # (coggan 2x8) — and it would break the locked pass-1 substring contract
    # (tests/test_library_filters.py::test_search_substring_match_name).
}

# Typo-rescue vocabulary: class words ONLY (bounded, no fuzzy over full names
# — cost + surprise). A token ≥5 chars that matches nothing anywhere is
# retried at Levenshtein distance ≤1 against these, then re-mapped through
# _SEARCH_SYNONYMS ("treshold" → threshold family).
_SEARCH_TYPO_VOCAB = ("threshold", "sweetspot", "endurance", "recovery", "tempo",
                      "anaerobic", "overunder", "neuromuscular", "ronnestad", "vo2max")

# Percent markers inside a row haystack: "@ 91%" (display names), "91%",
# "65pct" (legacy filenames). Used for the "@105" intensity intent (±2 pts).
_SEARCH_HAY_PCT_RX = re.compile(r"@\s*(\d{2,3})\b|(\d{2,3})\s*%|(\d{2,3})pct")
# 30s15s-shaped pattern anywhere in a haystack (ronnestad fallback).
_SEARCH_30S15S_RX = re.compile(r"(?<!\d)30s\s?15s(?![a-z0-9])")

_SEARCH_DUR_TOL = 0.12    # bare "90min" / "1h30" / "1.5h" → target ±12%
_SEARCH_QUERY_CAP = 300   # sanity cap: bound regex work on garbage input

def _search_lev1(a: str, b: str) -> bool:
    """True iff Levenshtein(a, b) ≤ 1. O(n) early-exit, no DP table."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if a == b:
        return True
    if la == lb:                       # exactly one substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:                        # make a the shorter one
        a, b, la = b, a, lb
    i = 0                              # one insertion into a
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]

def _search_parse_query(q: str) -> dict:
    """Parse a library search query into intent filters + residual AND-tokens.

    Pure (no I/O), importable by tests. Extraction order matters — each step
    CONSUMES its span so later steps never re-read it:
      1. comparators   "<60" ">120"          → literal duration bound
      2. explicit NsMs "30s15s"              → structure token
      3. slash pairs   "30/15" "40/20"       → structure (slash ≠ range, ever)
      4. hyphen pairs  "60-90" vs "30-15"    → ascending + plausible minutes =
                       duration range; descending/equal = on/off structure
                       (matches the spec examples: 30-15 structure, 60-90 range)
      5. NxM           "3x16" "3 x 16(min)"  → structure token, unit consumed
                       so the trailing "min" can't leak into step 7
      6. HhMM          "1h30"                → duration target ±12%
      7. bare duration "90min" "90m" "1.5h"  → duration target ±12%
      8. intensity     "@105" "@ 105%" "105%" → percent intent (±2 pts)
    The residue is normalized, bigram-scanned for two-word synonyms
    ("sweet spot"), then mapped token-by-token through _SEARCH_SYNONYMS.

    Returns dict with: phrase, tokens, families[(prefix, orig)],
    semantics[(kind, val, orig)], structures, _struct_rx (compiled),
    duration (lo, hi) | None, percent | None, intents (echo labels, the
    client prettifies), highlight (tokens the UI should <mark>).
    """
    out: dict = {
        "raw": q or "", "phrase": "", "tokens": [], "families": [],
        "semantics": [], "structures": [], "_struct_rx": [],
        "duration": None, "percent": None, "intents": [], "highlight": [],
    }
    s = (q or "").strip().lower().translate(_SEARCH_TRANSLIT)[:_SEARCH_QUERY_CAP]
    if not s:
        return out
    out["phrase"] = _search_normalize(s)
    dur_intents: list[str] = []      # duration echo (last duration intent wins)
    struct_seen: set[str] = set()

    def _add_struct(tok: str, rx: str, label: str) -> str:
        if tok not in struct_seen:
            struct_seen.add(tok)
            out["structures"].append(tok)
            out["_struct_rx"].append(re.compile(rx))
            out["intents"].append(label)
            out["highlight"].append(tok)
        return " "

    def _set_dur(lo: float, hi: float, label: str) -> str:
        out["duration"] = (lo, hi)   # last one wins — comment over engineering
        dur_intents.clear()
        dur_intents.append(label)
        return " "

    # 1) comparators — literal, half-open (>120 excludes 120.0 exactly).
    def _cmp(m: re.Match) -> str:
        v = float(m.group(2))
        if m.group(1) == "<":
            return _set_dur(0.0, v - 1e-6, f"<{m.group(2)}min")
        return _set_dur(v + 1e-6, float("inf"), f">{m.group(2)}min")
    s = re.sub(r"([<>])\s*(\d+(?:\.\d+)?)", _cmp, s)

    # 2) explicit NsMs ("30s15s", "30s 15s")
    def _nsms(on: int, off: int) -> str:
        return _add_struct(f"{on}s{off}s",
                           rf"(?<!\d){on}s\s?{off}s(?![a-z0-9])", f"{on}/{off}")
    s = re.sub(r"(?<!\d)(\d{1,3})\s*s\s*(\d{1,3})\s*s(?![a-z0-9])",
               lambda m: _nsms(int(m.group(1)), int(m.group(2))), s)

    # 3) slash pairs → always structure, normalized to the library's NsMs token.
    s = re.sub(r"(?<![\d.])(\d{1,3})\s*/\s*(\d{1,3})(?![\d.])",
               lambda m: _nsms(int(m.group(1)), int(m.group(2)))
               if 5 <= int(m.group(1)) <= 600 and 1 <= int(m.group(2)) <= 600
               else m.group(0), s)

    # 4) hyphen pairs: ascending + minute-plausible = duration range (60-90);
    #    otherwise on/off structure (30-15, 30-30). "1-2-3-2-1" pyramid names
    #    fail both guards and fall through as plain text.
    def _hyphen(m: re.Match) -> str:
        a, b = int(m.group(1)), int(m.group(2))
        if a < b and b >= 15:
            return _set_dur(float(a), float(b), f"{a}-{b}min")
        if a >= b and 5 <= a <= 600 and b >= 1:
            return _nsms(a, b)
        return m.group(0)
    s = re.sub(r"(?<![\d.-])(\d{1,3})\s*-\s*(\d{1,3})(?![\d.-])", _hyphen, s)

    # 5) NxM ("3x16", "4 x 8", "13x30", "3x16min" — unit consumed if present).
    def _nxm(m: re.Match) -> str:
        n, reps = int(m.group(1)), int(m.group(2))
        tok = f"{n}x{reps}"
        # Unit-agnostic haystack match: "3x16" hits "3x16min", "3x30" hits
        # "3x30s"; the digit guards stop "3x16" matching "3x160" / "13x30".
        return _add_struct(tok, rf"(?<!\d){n}x{reps}(?!\d)", tok)
    s = re.sub(r"(?<!\d)(\d{1,3})\s*x\s*(\d{1,4})\s*(?:mins?|m|s(?:ecs?)?)?(?![\w%])",
               _nxm, s)

    # 6) HhMM ("1h30") → minutes target ±12%.
    def _hhmm(m: re.Match) -> str:
        v = int(m.group(1)) * 60 + int(m.group(2))
        return _set_dur(v * (1 - _SEARCH_DUR_TOL), v * (1 + _SEARCH_DUR_TOL),
                        f"~{v}min")
    s = re.sub(r"(?<!\d)(\d{1,2})\s*h\s*(\d{1,2})(?![\dh%])", _hhmm, s)

    # 7) bare durations ("90min", "90m", "1.5h", "2 hours") → target ±12%.
    def _bare(m: re.Match) -> str:
        v = float(m.group(1))
        if m.group(2).startswith("h"):
            v *= 60
        v = round(v, 1)
        label = f"~{int(v) if v == int(v) else v}min"
        return _set_dur(v * (1 - _SEARCH_DUR_TOL), v * (1 + _SEARCH_DUR_TOL), label)
    s = re.sub(r"(?<![\dx.])(\d+(?:\.\d+)?)\s*(mins?|m|hours?|hrs?|h)(?![\w%])",
               _bare, s)

    # 8) intensity ("@105", "@ 105%", "105%") → ±2-pt percent intent.
    def _pct(m: re.Match) -> str:
        p = int(m.group(1) or m.group(2))
        if not 40 <= p <= 200:       # implausible as an FTP percent → leave as text
            return m.group(0)
        out["percent"] = p
        out["intents"].append(f"@{p}%")
        return " "
    s = re.sub(r"@\s*(\d{1,3})\s*%?|(?<![\dx.])(\d{1,3})\s*%", _pct, s)

    out["intents"].extend(dur_intents)

    # Residue → normalized tokens; bigram pass resolves two-word synonyms
    # BEFORE single tokens so "sweet spot" doesn't decay into "sweet"+"spot".
    words = _search_normalize(s).split()
    i = 0
    while i < len(words):
        pair = " ".join(words[i:i + 2]) if i + 1 < len(words) else None
        tok, step = (pair, 2) if pair and pair in _SEARCH_SYNONYMS else (words[i], 1)
        kind_val = _SEARCH_SYNONYMS.get(tok)
        if kind_val:
            kind, val = kind_val
            if kind == "family":
                out["families"].append((val, tok))
                out["intents"].append(val.replace("_", " "))
            else:
                out["semantics"].append((kind, val, tok))
                out["intents"].append(tok)
            out["highlight"].append(tok)
        elif tok:
            out["tokens"].append(tok)
            out["highlight"].append(tok)
        i += step
    return out

def _search_apply_typo_fix(parsed: dict, haystacks: list[str]) -> None:
    """Bounded typo rescue, mutating ``parsed`` in place.

    Only plain tokens ≥5 chars that hit NO haystack at all are retried, and
    only against the ten class-vocabulary words (Levenshtein ≤1); the winner
    re-enters through _SEARCH_SYNONYMS so "treshold" behaves exactly like
    "threshold". The any()-scan short-circuits on the first haystack hit.
    """
    if not parsed["tokens"]:
        return
    kept: list[str] = []
    for tok in parsed["tokens"]:
        if len(tok) < 5 or any(tok in h for h in haystacks):
            kept.append(tok)
            continue
        fix = next((v for v in _SEARCH_TYPO_VOCAB if _search_lev1(tok, v)), None)
        if fix is None:
            kept.append(tok)
            continue
        kind, val = _SEARCH_SYNONYMS[fix]
        if kind == "family":
            parsed["families"].append((val, fix))
        else:
            parsed["semantics"].append((kind, val, fix))
        parsed["intents"].append(f"{tok}→{fix}")
        # Highlight the CORRECTED word — the typo can't appear in any name.
        parsed["highlight"] = [fix if t == tok else t for t in parsed["highlight"]]
    parsed["tokens"] = kept

def _search_match_row(row: dict, hay: str, parsed: dict,
                      class_filter_active: bool = False) -> bool:
    """AND-match one cached library row against a parsed query."""
    dur = parsed["duration"]
    if dur is not None:
        d = row.get("Duration(min)") or 0
        if not (dur[0] <= d <= dur[1]):
            return False
    for rx in parsed["_struct_rx"]:
        if not rx.search(hay):
            return False
    if parsed["percent"] is not None:
        pcts = [int(g) for tup in _SEARCH_HAY_PCT_RX.findall(hay) for g in tup if g]
        pcts = [p for p in pcts if 40 <= p <= 200]
        if pcts:
            if not any(abs(p - parsed["percent"]) <= 2 for p in pcts):
                return False
        elif str(parsed["percent"]) not in hay:   # no embedded % → substring
            return False
    cls = (row.get("content_class") or "").lower()
    for prefix, orig in parsed["families"]:
        if class_filter_active:
            # Pass-1 guard kept: with an explicit Type filter active the
            # family token stays a plain substring, so Type=threshold +
            # "vo2" intersects sanely instead of guaranteeing 0 rows.
            if orig not in hay:
                return False
        elif not cls.startswith(prefix):
            return False
    for kind, val, orig in parsed["semantics"]:
        if kind == "class":
            ok = cls == val or orig in hay
        elif kind == "suffix":
            ok = cls.endswith(val) or orig in hay
        elif kind == "ftp_test":
            ok = cls == "ftp_test" or "ftp test" in hay or orig in hay
        elif kind == "ronnestad":
            # is_ronnestad tag (normalized "is ronnestad") OR 30/15 pattern.
            ok = ("is ronnestad" in hay or "ronnestad" in hay
                  or bool(_SEARCH_30S15S_RX.search(hay)))
        elif kind == "micro":
            ok = (bool((row.get("secondary_flags") or {}).get("pattern_microinterval"))
                  or "15s15s" in hay or "microburst" in hay or orig in hay)
        else:                                     # unknown kind → substring
            ok = orig in hay
        if not ok:
            return False
    for tok in parsed["tokens"]:
        if tok not in hay:
            return False
    return True

def _search_score_row(row: dict, hay: str, parsed: dict) -> int:
    """Relevance points for an already-matched row (higher = better).

    Additive tiers per the pass-2 spec: exact-phrase-in-name 100 >
    all-tokens-in-name 60 > structure hit 50 > class/family hit 30 >
    token-prefix-in-name 20 > file/tag-only floor 10. Ties break on
    duration asc (the caller's sort key).
    """
    name_norm = _search_normalize(
        (row.get("display_name") or "") + " " + (row.get("Name") or ""))
    score = 10                                   # matched at all (file/tag tier)
    phrase = parsed["phrase"]
    if len(phrase) >= 4 and phrase in name_norm:
        score += 100
    wordy = (parsed["tokens"]
             + [orig for _p, orig in parsed["families"]]
             + [orig for _k, _v, orig in parsed["semantics"]])
    if wordy and all(w in name_norm for w in wordy):
        score += 60
    if parsed["_struct_rx"] and any(rx.search(name_norm) for rx in parsed["_struct_rx"]):
        score += 50
    if parsed["families"] or parsed["semantics"]:
        score += 30
    if wordy:
        name_words = name_norm.split()
        if any(word.startswith(w) for w in wordy for word in name_words):
            score += 20
    return score
