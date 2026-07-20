"""v3.4.4 — Plan GRID view: information parity with the Calendar view.

Owner (screenshots compared): grid cards showed only a bare type chip
("VO2"/"Z2"), "60m", an unlabeled zone sprite and a cryptic 1-8 number
badge, while calendar cards show the matched workout's structure title,
"60m · 45 TSS", warning glyph, redraw and missed/done states.

The number badge was `session.score` — the score_workout() 1-10 curation
heuristic surfaced by _enrich_plan_for_response from the library index
Score column. The owner removed the same number from the library table
(e69a07a0, 2026-07-07) as "an arbitrary curation heuristic"; the grid was
the last surface still leaking it. It is gone from grid cards.

Grid cards now source their content from plannedCardParts — the SAME
helper renderCalDay consumes — so the two plan views cannot drift on what
a card says. The grid keeps its density (smaller type, 2-line clamp).

Sprite sanity: the mini zone sprite only renders from `zone_dist` when the
session actually has a matched zwo_file (zone_dist is enriched per-file
server-side; a fileless slot can carry stale zone data — the owner caught
a "Z2 60m" card with hard blocks whose modal said Recovery). Fileless /
unenriched sessions fall back to the session_type silhouette, explicitly
labeled approximate in the tooltip.

Calendar output is PINNED byte-for-byte: the renderCalDay refactor onto
plannedCardParts must not change a single character of calendar cells
(score badge included — the calendar is not in scope of the removal).
"""
import json
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
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


def _extract_const(src: str, name: str) -> str:
    i = src.index(f"const {name}")
    j = src.index("};", i)
    return src[i:j + 2]


def _run_node(harness: str) -> str:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"
    return res.stdout


_ESC = ("const esc = s => String(s==null?'':s).replace(/&/g,'&amp;')"
        ".replace(/</g,'&lt;').replace(/>/g,'&gt;')"
        ".replace(/\"/g,'&quot;').replace(/'/g,'&#39;');")

_SHARED_FNS = "\n".join([
    _extract_const(SRC, "CAL_CONTENT_LABEL"),
    _extract_const(SRC, "CAL_SESSION_LABEL"),
    _extract_const(SRC, "CAL_SESSION_CSS"),
    _ESC,
    _extract_js_function(SRC, "calContentCss"),
    _extract_js_function(SRC, "calCardTitle"),
    _extract_js_function(SRC, "calStructureSuffix"),
    _extract_js_function(SRC, "calCardTitleWithStructure"),
    _extract_js_function(SRC, "plannedCardParts"),
    _extract_js_function(SRC, "pgZoneTooltip"),
])


# ── Calendar pin ─────────────────────────────────────────────────────────
# Captured from renderCalDay at 26a13476 (pre-refactor HEAD) for the five
# fixture days below. The refactor onto plannedCardParts must reproduce
# these cells byte-for-byte.
_CAL_PIN = json.loads(r"""
[
"<div class=\"cal-day cal-phase-build1\" data-cs=\"planned\" data-cs-v2=\"planned\"\n    data-date=\"2026-07-21\" data-week-idx=\"2\" data-day-idx=\"0\"\n    data-cal-rest=\"0\" data-cal-completed=\"0\" data-cal-missing=\"0\"\n    draggable=\"true\" ondragstart=\"calDragStart(event,'2026-07-21',2)\" ondragend=\"calDragEnd(event)\" title=\"VO2max 4x3min (60min) · class=vo2max · vo2max_4x3_60min.zwo · 60m · score=7/10\"\n    ondragover=\"calDragOver(event)\" ondragleave=\"calDragLeave(event)\" ondrop=\"calDrop(event,'2026-07-21',2)\"\n    onclick=\"calOpenDay('2026-07-21')\">\n    <span class=\"cal-redraw-btn\" title=\"Swap workout — same type, different session\"\n      onclick=\"event.stopPropagation();calRedrawDay('2026-07-21', this);\">⟳</span>\n    \n    <div class=\"cal-planned\">\n      <span class=\"cal-planned-type wc-hit\">VO2max — 2×/3min @ 105% · 4×3</span>\n      \n      <div class=\"cal-planned-meta\">60m · 45 TSS</div>\n    </div><span class=\"pg-score-badge pg-score-gold\" title=\"Score 7/10 — Good\">7</span>\n    \n  </div>",
"<div class=\"cal-day cal-phase-build1\" data-cs=\"planned\" data-cs-v2=\"planned\"\n    data-date=\"2026-07-15\" data-week-idx=\"2\" data-day-idx=\"1\"\n    data-cal-rest=\"0\" data-cal-completed=\"0\" data-cal-missing=\"0\"\n    draggable=\"true\" ondragstart=\"calDragStart(event,'2026-07-15',2)\" ondragend=\"calDragEnd(event)\" title=\"Z2 Endurance (90min) · class=endurance · endurance_90min.zwo · 90m · score=4/10\"\n    ondragover=\"calDragOver(event)\" ondragleave=\"calDragLeave(event)\" ondrop=\"calDrop(event,'2026-07-15',2)\"\n    onclick=\"calOpenDay('2026-07-15')\">\n    <span class=\"cal-redraw-btn\" title=\"Swap workout — same type, different session\"\n      onclick=\"event.stopPropagation();calRedrawDay('2026-07-15', this);\">⟳</span>\n    \n    <div class=\"cal-planned\">\n      <span class=\"cal-planned-type wc-z2\">Z2 Endurance · steady</span>\n      \n      <div class=\"cal-planned-meta\">90m · 60 TSS</div>\n    </div><span class=\"pg-score-badge pg-score-medium\" title=\"Score 4/10 — Medium\">4</span>\n    <div class=\"cal-actual cal-act-red\">\n      <div class=\"cal-actual-meta\" style=\"font-style:italic;color:var(--red);\">missed</div>\n    </div>\n  </div>",
"<div class=\"cal-day cal-phase-build1 cal-completed\" data-cs=\"completed\" data-cs-v2=\"completed\"\n    data-date=\"2026-07-14\" data-week-idx=\"2\" data-day-idx=\"2\"\n    data-cal-rest=\"0\" data-cal-completed=\"1\" data-cal-missing=\"0\"\n     title=\"Sweet Spot 3x15 (75min) · class=sweet_spot · ss_3x15_75min.zwo · 75m · score=8/10 · actual: Morning Ride\"\n    ondragover=\"calDragOver(event)\" ondragleave=\"calDragLeave(event)\" ondrop=\"calDrop(event,'2026-07-14',2)\"\n    onclick=\"calOpenDay('2026-07-14')\">\n    \n    \n    <div class=\"cal-planned\">\n      <span class=\"cal-planned-type wc-z2\">Sweet Spot 3×15 · 3×15</span>\n      \n      <div class=\"cal-planned-meta\">75m · 80 TSS</div>\n    </div><span class=\"pg-score-badge pg-score-gold\" title=\"Score 8/10 — Good\">8</span>\n    <div class=\"cal-actual cal-act-green pol-base\" style=\"border-left-color:var(--green);\">\n      <div class=\"cal-actual-name\">Morning Ride <span class=\"cal-match-badge\" data-match=\"matched\"\n    title=\"duration ok\"\n    style=\"display:inline-block;font-size:10px;font-weight:700;line-height:1;\n           padding:1px 4px;margin-left:4px;border-radius:6px;\n           background:var(--green)22;color:var(--green);border:1px solid var(--green);\n           vertical-align:middle;\">✓</span></div>\n      <div class=\"cal-actual-meta\">78m · 82 TSS</div>\n    </div>\n  </div>",
"<div class=\"cal-day cal-phase-build1 cal-missing\" data-cs=\"missing_workout\" data-cs-v2=\"missing_workout\"\n    data-date=\"2026-07-23\" data-week-idx=\"2\" data-day-idx=\"3\"\n    data-cal-rest=\"0\" data-cal-completed=\"0\" data-cal-missing=\"1\"\n     title=\"60m\"\n    ondragover=\"calDragOver(event)\" ondragleave=\"calDragLeave(event)\" ondrop=\"calDrop(event,'2026-07-23',2)\"\n    onclick=\"calOpenDay('2026-07-23')\">\n    <span class=\"cal-redraw-btn cal-redraw-prominent\" title=\"Swap workout — same type, different session\"\n      onclick=\"event.stopPropagation();calRedrawDay('2026-07-23', this);\">⟳</span>\n    \n    <div class=\"cal-planned\">\n      <span class=\"cal-planned-type wc-hit\">THRESHOLD</span>\n      <span class=\"cal-planned-warn\" title=\"Workout missing — click ⟳ to assign one.\">⚠</span>\n      <div class=\"cal-planned-meta\">60m · 70 TSS</div>\n    </div>\n    \n  </div>",
"<div class=\"cal-day cal-rest cal-phase-build1\" data-cs=\"rest\" data-date=\"2026-07-24\">\n      <div class=\"cal-rest-label\" style=\"font-size:10px;font-weight:700;color:var(--text3);letter-spacing:0.05em;\">REST</div>\n    </div>"
]
""")

_CAL_FIXTURE_DAYS = """
const days = [
  { date: '2026-07-21', card_state: 'planned', is_today: false,
    planned: { session_type:'vo2max', content_class:'vo2max',
      name:'VO2max 4x3min (60min)', display_name:'VO2max — 2×3min/3min @ 105%',
      zwo_name:'VO2max 4x3min', duration_min:60, tss:45, score:7,
      zwo_file:'vo2max_4x3_60min.zwo' },
    actual: null },
  { date: '2026-07-15', card_state: 'planned', is_today: false,
    planned: { session_type:'z2', content_class:'endurance',
      name:'Z2 Endurance (90min)', display_name:'Z2 Endurance',
      zwo_name:'Z2 Endurance', duration_min:90, tss:60, score:4,
      zwo_file:'endurance_90min.zwo' },
    actual: null },
  { date: '2026-07-14', card_state: 'completed', is_today: false,
    planned: { session_type:'sweetspot', content_class:'sweet_spot',
      name:'Sweet Spot 3x15 (75min)', display_name:'Sweet Spot 3×15',
      zwo_name:'Sweet Spot 3x15', duration_min:75, tss:80, score:8,
      zwo_file:'ss_3x15_75min.zwo' },
    actual: { name:'Morning Ride', duration_min:78, tss:82,
      classification:'base',
      compare:{ match_status:'matched', reasons:['duration ok'] } } },
  { date: '2026-07-23', card_state: 'missing_workout', is_today: false,
    planned: { session_type:'threshold', content_class:'',
      name:'', display_name:'', zwo_name:'', duration_min:60, tss:70,
      score:null, zwo_file:'' },
    actual: null },
  { date: '2026-07-24', card_state: 'rest', is_today: false,
    planned: null, actual: null },
];
"""


def test_calendar_cells_pinned():
    """renderCalDay output is byte-identical to the pre-refactor capture."""
    harness = (
        "const window = { _calData: null };\n"
        + _SHARED_FNS + "\n"
        + _extract_js_function(SRC, "calCellTooltip") + "\n"
        + _extract_js_function(SRC, "calActualClass") + "\n"
        + _extract_js_function(SRC, "_classifColor") + "\n"
        + _extract_js_function(SRC, "_calMatchBadge") + "\n"
        + _extract_js_function(SRC, "renderCalDay") + "\n"
        + _CAL_FIXTURE_DAYS
        + "const out = days.map((d, i) => renderCalDay(d, 2, i, 'build1', '2026-07-20'));\n"
        + "console.log(JSON.stringify(out));\n"
    )
    got = json.loads(_run_node(harness))
    assert got == _CAL_PIN, "calendar cell output drifted from the pin"
    # The calendar keeps its score badge (removal was grid-only scope).
    assert "pg-score-badge" in got[0]


# ── Grid harness ─────────────────────────────────────────────────────────
# Runs the REAL renderPlanJSON against a 1-week fixture plan built around
# node's current date (renderPlanJSON reads the real clock): a missed past
# day, a completed past day (activity via the /api/activities stub), a
# future matched session and a future missing-workout slot carrying a
# STALE hard zone_dist with no file.
_GRID_HARNESS = (
    "const els = {};\n"
    "const $ = id => els[id] || (els[id] = { innerHTML: '', style: {} });\n"
    "const window = { _volUnit: 'h' };\n"
    "const PHASE_COLORS = {};\n"
    "function _syncVolUnitBtn() {}\n"
    "function pgRenderProgressHeader() {}\n"
    "function pgAutoScrollToCurrentWeek() {}\n"
    + _SHARED_FNS + "\n"
    + _extract_js_function(SRC, "volFmt") + "\n"
    + _extract_js_function(SRC, "_isCyclingSport") + "\n"
    + _extract_js_function(SRC, "buildPowerBlocks") + "\n"
    + _extract_js_function(SRC, "buildPowerBlocksFromZoneDist") + "\n"
    + _extract_js_function(SRC, "miniPowerBlockSVG") + "\n"
    # _extract_js_function keys on "function <name>" and so drops the
    # `async` prefix — restore it (renderPlanJSON awaits fetch).
    + "async " + _extract_js_function(SRC, "renderPlanJSON") + "\n"
    + r"""
const iso = d => d.toLocaleDateString('en-CA');
const shift = n => { const d = new Date(); d.setDate(d.getDate() + n); return d; };
const dMissed = iso(shift(-2)), dDone = iso(shift(-1));
const dPlanned = iso(shift(1)), dMissing = iso(shift(2));
const wStart = iso(shift(-3)), wEnd = iso(shift(3));

const fetch = async (url) => ({
  ok: true,
  json: async () => (url === '/api/activities'
    ? [{ date: dDone, sport: 'Ride', tss: 82, duration_min: 78 }]
    : []),
});

const mkSession = (day, extra) => Object.assign({
  day, day_name: 'X', session_type: 'z2', duration_min: 60,
  tss_estimate: 45, description: '', zwo_file: '', zwo_name: '',
  display_name: '', content_class: '', zone_dist: null, score: 8,
  status: 'pending', card_state: 'planned',
}, extra);

const plan = {
  goal: { type: 'event', event_name: 'Race', hours_per_week: 8 },
  phases: [{ name: 'build1', weeks: 1 }],
  weeks: [{
    start: wStart, end: wEnd, week_num: 1, phase: 'build1',
    is_stepback: false,
    sessions: [
      mkSession(dMissed, { session_type: 'threshold', tss_estimate: 70,
        zwo_file: 'thr_2x20_60min.zwo', zwo_name: 'Threshold 2x20 (60min)',
        display_name: 'Threshold 2×20 @ 98%', content_class: 'threshold',
        zone_dist: { z1: 20, z2: 25, z4: 55 } }),
      mkSession(dDone, { session_type: 'sweetspot', tss_estimate: 80,
        duration_min: 75, card_state: 'completed',
        zwo_file: 'ss_3x15_75min.zwo', zwo_name: 'Sweet Spot 3x15 (75min)',
        display_name: 'Sweet Spot 3×15 @ 90%', content_class: 'sweet_spot',
        zone_dist: { z1: 25, z2: 30, z3: 45 } }),
      mkSession(dPlanned, { session_type: 'vo2max', tss_estimate: 45,
        zwo_file: 'vo2max_4x3_60min.zwo', zwo_name: 'VO2max 4x3min (60min)',
        display_name: 'VO2max — 4×3 @ 110%', content_class: 'vo2max',
        zone_dist: { z1: 20, z2: 30, z5: 50 } }),
      // stale zone_dist, NO matched file → sprite must NOT use it
      mkSession(dMissing, { card_state: 'missing_workout', score: null,
        zone_dist: { z5: 60, z6: 40 } }),
    ],
  }],
};

(async () => {
  await renderPlanJSON(plan);
  const html = els['plan-calendar'].innerHTML;
  const expected = {
    plannedTitle: esc(calCardTitleWithStructure({
      session_type: 'vo2max', content_class: 'vo2max',
      display_name: 'VO2max — 4×3 @ 110%', zwo_name: 'VO2max 4x3min (60min)',
      name: 'VO2max 4x3min (60min)', duration_min: 60, tss: 45 })),
  };
  console.log(JSON.stringify({ html, dMissed, dDone, dPlanned, dMissing, expected }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
)


def _grid_render():
    out = json.loads(_run_node(_GRID_HARNESS))
    html = out["html"]
    # Slice per-card chunks keyed by data-pg-day.
    def chunk(day):
        marker = f'data-pg-day="{day}"'
        i = html.index(marker)
        j = html.find('data-pg-day="', i + len(marker))
        return html[i:j] if j != -1 else html[i:]
    chunks = {}
    for k in ("dMissed", "dDone", "dPlanned", "dMissing"):
        try:
            chunks[k] = chunk(out[k])
        except ValueError:
            chunks[k] = ""
    return out, html, chunks


def test_grid_card_has_name_duration_tss():
    out, html, chunks = _grid_render()
    c = chunks["dPlanned"]
    # Title = the calendar's own title cascade (shared helper) on the chip.
    assert out["expected"]["plannedTitle"] in c
    assert "wc-type" in c and "wc-hit" in c  # type-colored chip retained
    # Calendar-format meta line.
    assert "60m · 45 TSS" in c
    assert 'data-cs="planned"' in c
    # Redraw affordance present on planned cards.
    assert "pg-redraw-btn" in c


def test_grid_missed_done_states():
    out, html, chunks = _grid_render()
    m = chunks["dMissed"]
    assert ">missed<" in m and "font-style:italic" in m  # calendar wording
    assert "pg-card-actual" in m
    d = chunks["dDone"]
    assert 'data-pg-completed="1"' in d
    assert 'data-cs="completed"' in d
    assert "78m · 82 TSS" in d           # actual duration · TSS, like calendar
    # Green state tint class on the cell (class attr precedes the chunk key).
    assert 'class="wc-day cal-completed"' in html
    assert "pg-redraw-btn" not in d      # suppressed on completed


def test_grid_missing_workout_warn():
    out, html, chunks = _grid_render()
    c = chunks["dMissing"]
    assert 'data-cs="missing_workout"' in c
    assert "cal-planned-warn" in c and "⚠" in c
    assert "cal-redraw-prominent" in c


def test_grid_no_score_badge():
    out, html, chunks = _grid_render()
    assert "pg-score-badge" not in html
    # Static guard: the grid renderer source itself carries no score badge.
    grid_src = _extract_js_function(SRC, "renderPlanJSON")
    assert "pg-score-badge" not in grid_src
    assert "scoreBadgeHtml" not in grid_src
    # The calendar renderer keeps its badge (pinned above; scope was grid-only).
    assert "pg-score-badge" in _extract_js_function(SRC, "renderCalDay")


def test_grid_sprite_source_is_matched_file():
    out, html, chunks = _grid_render()
    # Matched session: sprite from the FILE's zone_dist, tooltip names zones.
    c = chunks["dPlanned"]
    assert "Matched workout zone mix" in c
    assert "Z5 VO2 50%" in c
    assert "#dc2626" in c  # z5 (113% FTP) bar drawn from the file's zones
    # Fileless slot with stale hard zone_dist: sprite must NOT render the
    # stale zones — falls back to the type silhouette, labeled approximate.
    s = chunks["dMissing"]
    assert "approximate" in s
    assert "Matched workout zone mix" not in s
    assert "#dc2626" not in s and "#ef4444" not in s  # no hard bars leaked
    # Sprite wrapper present with a tooltip on every planned card.
    assert 'class="pg-card-sprite" title="' in c
    assert 'class="pg-card-sprite" title="' in s
