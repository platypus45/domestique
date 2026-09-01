"""3.4.3 B1+B2 — day-modal today-identity clarity + action-cluster info icons.

Owner incident: the Training Plan day modal titled a Thursday session just
"Thursday — THRESHOLD (61min)", which read as TODAY's session next to the
home card (a navigation artifact — the data layer agreed everywhere). The
modal title now makes the day unmistakable:

- today        → "Today — <TYPE> (<N>min)"
- any other day → "<Weekday> <D> <Mon> — <TYPE>" + a relative pill
                  ("in 3 days" / "tomorrow" / "yesterday" / "N days ago")

B2: the "Change the type…" (i) did nothing — it had NO data-popover, so the
document-level delegated handler ignored it. Each of the three cluster
buttons now carries its own wired (i): rematch / swap-type / easier.
"""
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"


# ═══════════════════════════════════════════════════════════════════════════
# B1 — _dayModalTitle: Today / weekday+date + relative pill
# ═══════════════════════════════════════════════════════════════════════════

def test_day_modal_title_today_and_relative_pills():
    # Server today pinned to 2026-07-16 (a Thursday) via _calTodayStr's
    # server-truth branch; real Date handles the local-noon date math.
    harness = """
const window = { _calData: { today: '2026-07-16' } };
""" + (_extract_js_function(SRC, "_calTodayStr") + "\n"
       + _extract_js_function(SRC, "_dayModalTitle")) + """
function expect(day, label, pillPart) {
  const t = _dayModalTitle(day, 'Fallbackday');
  if (t.label !== label) throw new Error(day + ': label ' + t.label + ' != ' + label);
  if (!pillPart && t.pill !== '') throw new Error(day + ': expected no pill, got ' + t.pill);
  if (pillPart && t.pill.indexOf('>' + pillPart + '<') < 0)
    throw new Error(day + ': pill ' + t.pill + ' missing ' + pillPart);
  if (pillPart && t.pill.indexOf('day-rel-pill') < 0)
    throw new Error(day + ': pill must use the day-rel-pill class');
}
expect('2026-07-16', 'Today', null);                       // today, no pill
expect('2026-07-19', 'Sunday 19 Jul', 'in 3 days');        // the owner case class
expect('2026-07-17', 'Friday 17 Jul', 'tomorrow');
expect('2026-07-15', 'Wednesday 15 Jul', 'yesterday');
expect('2026-07-13', 'Monday 13 Jul', '3 days ago');
// no parseable date → day_name fallback, no pill (synthetic sessions)
const fb = _dayModalTitle('', 'Monday');
if (fb.label !== 'Monday' || fb.pill !== '') throw new Error('fallback broken: ' + JSON.stringify(fb));
console.log('OK');
"""
    _run_node(harness)


def test_day_modal_title_is_server_truth_not_client_clock():
    # The "Today" decision keys on the SERVER-provided date — a skewed
    # client clock (or the UTC midnight window) must not relabel days.
    harness = """
const window = { _calData: { today: '2026-07-16' } };
""" + (_extract_js_function(SRC, "_calTodayStr") + "\n"
       + _extract_js_function(SRC, "_dayModalTitle")) + """
const t = _dayModalTitle('2026-07-16', 'Thursday');
if (t.label !== 'Today') throw new Error('server-marked today must title Today, got ' + t.label);
console.log('OK');
"""
    _run_node(harness)


def test_modal_h2_sites_use_title_helper():
    # Both h2 sites (adjusted-rest branch + main hero) render the identity
    # label + pill; the old bare `${session.day_name} — ` title is gone.
    assert "<h2>${esc(_title.label)} — ${esc(heroTitle)}${_title.pill}</h2>" in SRC
    assert "<h2>${esc(_title.label)} — Rest day${_title.pill}</h2>" in SRC
    assert "<h2>${session.day_name} — " not in SRC
    # The today-only verbs (easier / "Skip today") key on the same unified
    # today string as the title — one definition of today in the modal.
    assert "String(session.day) === _calTodayStr()" in SRC
    # Pill CSS exists.
    assert ".day-rel-pill" in SRC


# ═══════════════════════════════════════════════════════════════════════════
# B2 — every cluster button has its own wired (i)
# ═══════════════════════════════════════════════════════════════════════════

def test_all_three_cluster_icons_wired_to_existing_popovers():
    # Each button is immediately followed by an info-icon whose data-popover
    # names a popover div that actually exists (the dead-icon root cause was
    # the swap icon carrying NO data-popover at all). 3.4.3 owner relabel:
    # buttons self-explanatory without the (i)s.
    for btn, pop in (
        ("Swap workout — same type, different session</button>",
         "planpop-rematch"),
        ("Change training type (VO2, tempo, &hellip;)</button>",
         "planpop-swaptype"),
        ("Make it easier today</button>", "planpop-easier"),
    ):
        i = SRC.index(btn)
        tail = SRC[i:i + 400]
        assert f'data-popover="{pop}"' in tail, f"button {btn!r} lacks its (i) → {pop}"
        assert f'id="{pop}"' in SRC, f"popover div {pop} missing"
    # No bare info-icon (missing data-popover) inside the cluster block.
    ci = SRC.index("Change this workout")
    cluster = SRC[ci:ci + 1400]
    for chunk in cluster.split('class="info-icon"')[1:]:
        assert "data-popover=" in chunk[:120], "cluster info-icon without data-popover (dead icon)"


# ═══════════════════════════════════════════════════════════════════════════
# 3.4.3 owner fix — structural popover invariants (the CLASS of bug, not the
# instance): (a) no data-popover may dangle (its target div must exist);
# (b) a popover referenced from JS-rendered content (the modal) must live at
# BODY level, never inside a .section — sections are display:none when their
# tab is inactive, so a section-buried popover gets .open added but renders
# 0x0 (owner incident: the swap-type (i) was dead when the day modal was
# opened from the Home today-card; from the Plan tab it "live-proved" fine).
# ═══════════════════════════════════════════════════════════════════════════

def _popover_refs_by_context():
    """All data-popover refs in the template, split into (static-HTML refs,
    refs inside <script> blocks — i.e. rendered into modals/dynamic DOM)."""
    import re
    script_spans = [m.span() for m in
                    re.finditer(r"<script\b.*?</script>", SRC, re.S)]

    def _in_script(pos):
        return any(a <= pos < b for a, b in script_spans)

    static, scripted = set(), set()
    for m in re.finditer(r'data-popover="([^"]+)"', SRC):
        (scripted if _in_script(m.start()) else static).add(m.group(1))
    assert static and scripted, "popover refs vanished — pattern drifted"
    return static, scripted


def _section_spans():
    """Byte spans of every <div class="section" ...> block, matched by real
    div nesting. Div tags INSIDE <script> blocks (JS template literals full
    of html += '<div…>') are skipped — they aren't static DOM and would
    corrupt the nesting stack."""
    import re
    script_spans = [m.span() for m in
                    re.finditer(r"<script\b.*?</script>", SRC, re.S)]

    def _in_script(pos):
        return any(a <= pos < b for a, b in script_spans)

    spans = []
    opens = []  # stack of (offset, is_section) per open static <div>
    for m in re.finditer(r"<div\b[^>]*>|</div\s*>", SRC):
        if _in_script(m.start()):
            continue
        tag = m.group(0)
        if tag.startswith("</"):
            if opens:
                start, is_sec = opens.pop()
                if is_sec:
                    spans.append((start, m.end()))
        else:
            is_sec = bool(re.search(r'class="[^"]*\bsection\b', tag))
            opens.append((m.start(), is_sec))
    assert spans, "no .section blocks found — selector drifted"
    return spans


def test_no_dangling_popover_refs():
    # EVERY data-popover="X" — static or modal-rendered — must have a
    # matching id="X" div. A dangling ref fails silently in the delegated
    # handler (getElementById → null → return), so only a test catches it.
    static, scripted = _popover_refs_by_context()
    for ref in sorted(static | scripted):
        assert f'id="{ref}"' in SRC, f"data-popover={ref!r} dangles (no div)"


def test_modal_referenced_popovers_are_body_level_not_section_buried():
    # Popovers referenced from <script> template strings render inside the
    # fixed modal overlay, which is visible from EVERY tab — their target
    # divs must therefore sit outside all .section containers (precedent:
    # calpush-info). A section-buried target is the invisible-open bug.
    _, scripted = _popover_refs_by_context()
    spans = _section_spans()
    for ref in sorted(scripted):
        i = SRC.index(f'id="{ref}"')
        buried = [s for s, e in spans if s <= i < e]
        assert not buried, (
            f"popover {ref!r} is inside a .section (display:none when that "
            f"tab is inactive) but is referenced from modal-rendered JS — "
            f"move its div next to the modal overlay (see calpush-info)")
    # The three cluster popovers are modal-referenced — keep them covered by
    # this invariant forever (they were the section-buried instance).
    for pid in ("planpop-rematch", "planpop-swaptype", "planpop-easier"):
        assert pid in scripted, f"{pid} no longer referenced from the modal?"


def test_cluster_popover_copy_locked():
    # The three popovers explain the consequence in rider terms (locked copy).
    for pid, phrase in (
        ("planpop-rematch",
         "swaps in another workout of the same type and duration — your plan's load stays the same"),
        ("planpop-swaptype",
         "pick a different training type for this day — the week re-balances around it"),
        ("planpop-easier",
         "drops today one intensity notch (e.g. threshold → sweet spot) — use when you're not feeling it"),
    ):
        i = SRC.index(f'id="{pid}"')
        div = SRC[i:SRC.index("</div>", i)]
        assert phrase in div, f"{pid} copy drifted: {div[:200]}"


def test_delegated_handler_covers_dynamic_modal_content():
    # The popover handler is delegated on DOCUMENT (not bound per-element),
    # so icons inside innerHTML-inserted modal content work. Pin the two
    # load-bearing pieces: document-level click listener + closest lookup.
    i = SRC.index("initInfoPopovers")
    block = SRC[i:i + 2200]
    assert "document.addEventListener('click'" in block
    assert "closest('[data-popover]')" in block.replace('e.target.closest && e.target.closest', "closest"), \
        "delegated handler must resolve icons via closest([data-popover])"


def test_plan_open_sequence_refreshes_today_session():
    # B1 root fix (owner incident, forensics 2026-07-16): the plan-open
    # sequence's step 4 can REWRITE today's session (missed-hard refit /
    # rebuild / rebalance) — observed live as home card VO2MAX (pre-refit
    # generation) vs plan-modal THRESHOLD (post-refit) for the SAME day.
    # The sequence must re-pull /api/today-session afterwards so the home
    # card and window._todaySessionData can never keep serving the
    # pre-update generation. Pinned in BOTH finally branches.
    i = SRC.index("async function runPlanOpenSequence")
    j = SRC.index("\n// v1.8.24 — ONE adaptation path for the UI", i)
    body = SRC[i:j]
    fin = body[body.index("} finally {"):]
    assert fin.count("loadTodaySession()") >= 2, (
        "runPlanOpenSequence must refresh today-session after the plan "
        "update in both finally branches (stale home-card divergence)")
    # On success the refresh lands before the overlay dismisses.
    ok_branch = fin[fin.index("} else {"):]
    assert ok_branch.index("loadTodaySession()") < ok_branch.index("dismissPlanCatchup()")
