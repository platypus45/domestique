"""3.4.1 M2 — today-card + Analysis-tab presentational fixes.

Node-harness tests over templates/dashboard.html (the extract-function
pattern from test_331_surfaces / test_340_continuous_w3), one section per
M2 item:

1  fatigue-card-always-open — Fatigue Resistance is its OWN always-open
   card (Power Curve card pattern); the <details>/<summary> fold and its
   ontoggle lazy loader are retired; loadAnalysisTab() owns the load;
   refreshFatigueResistance() works without the <details> force-open.
2  centered-preview — the today-card blocks preview centers horizontally
   (margin-inline auto) with the 560px width cap preserved.
3  contrast — kicker / duration line / Now-line / "Approximate shape"
   caption sit at --text2 (was --text3), and the preview-chart axis labels
   are bumped via a scoped CSS override that holds in BOTH theme roots.
4  banner-single-reason — the adjustment banner renders the engine's
   plain-sentence reason EXACTLY once, with no "≥8"-style internal
   notation anywhere, and a Now-line that no longer repeats the reason.
   (The engine-side copy itself is pinned in test_r4r5_engine.py.)

Hermetic: template text + node only; no app import, no network.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "templates" / "dashboard.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Slice `[async ]function <name>(...) {...}` by brace count (the
    test_331_surfaces extractor with the open-paren anchor)."""
    start = src.find(f"async function {name}(")
    if start < 0:
        start = src.index(f"function {name}(")
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


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "OK" in res.stdout


# Real-ish esc for the render pins (mirror of the dashboard's own escaper).
_ESC_STUB = """
const esc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
"""


# ═══════════════════════════════════════════════════════════════════════════
# 1 — Fatigue Resistance: own always-open card, fold + toggle handlers retired
# ═══════════════════════════════════════════════════════════════════════════

def test_fatigue_resistance_is_own_always_open_card():
    src = _src()
    # Own card, Power-Curve pattern; the <details> fold is gone.
    assert '<div class="card fatigue-resistance" id="fatigue-resistance-panel"' in src
    assert '<details class="fatigue-resistance"' not in src
    # The 3.2.1-era lazy ontoggle loader is retired with the wrapper.
    assert 'ontoggle="if(this.open)loadFatigueResistance()"' not in src
    # Inner loader surface intact (every id the loader/renderer keys on).
    for pid in (
        'id="fatigue-resistance-content"', 'id="fatigue-resistance-body"',
        'id="fatigue-resistance-headline"',
        'id="fatigue-resistance-refresh-btn"',
        'id="fr-kj-1500"', 'id="fr-kj-2000"',
        'id="fatigue-resistance-info-icon"',
        'id="fatigue-resistance-info-popover"',
    ):
        assert pid in src, f"{pid} lost in the card conversion"
    # Sibling <details> panels (τ-fit / model-accuracy) keep their folds.
    assert 'ontoggle="if(this.open)loadTauFitPanel()"' in src
    assert 'ontoggle="if(this.open)loadBanisterValidationPanel()"' in src


def test_analysis_tab_loader_owns_the_fatigue_load():
    """Always-open card ⇒ the tab loader fires it (same as the power curve)."""
    src = _src()
    fn = _extract_js_function(src, "loadAnalysisTab")
    assert "loadPowerCurve" in fn          # unchanged sibling
    assert "loadFatigueResistance" in fn, \
        "loadAnalysisTab must load the always-open fatigue card"


def test_refresh_no_longer_references_the_details_state():
    """The <details> force-open (panel.open = true) is retired cleanly —
    refresh keys only on the button now."""
    src = _src()
    fn = _extract_js_function(src, "refreshFatigueResistance")
    assert "fatigue-resistance-panel" not in fn
    assert ".open" not in fn
    # The refresh button's summary-click guards died with the <summary>.
    btn = src[src.index('id="fatigue-resistance-refresh-btn"'):]
    btn = btn[:btn.index("</button>")]
    assert "stopPropagation" not in btn
    assert "preventDefault" not in btn


@needs_node
def test_refresh_still_forces_recompute_without_details():
    src = _src()
    fn = _extract_js_function(src, "refreshFatigueResistance")
    harness = """
let forced = null;
const btn = { disabled: false, textContent: 'Refresh ⟳' };
const document = { getElementById: id =>
  (id === 'fatigue-resistance-refresh-btn' ? btn : null) };
const loadFatigueResistance = async (force) => {
  forced = force;
  if (btn.textContent !== 'Refreshing…') throw new Error('busy label missing');
  if (!btn.disabled) throw new Error('button must disable while refreshing');
};
""" + fn + """
refreshFatigueResistance().then(() => {
  if (forced !== true) throw new Error('must force a ?refresh=1 recompute');
  if (btn.disabled) throw new Error('button must re-enable');
  if (btn.textContent !== 'Refresh ⟳') throw new Error('label must restore');
  console.log('OK');
}).catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# 2 + 3 — centered preview + contrast pins (both themes)
# ═══════════════════════════════════════════════════════════════════════════

def test_preview_wrapper_centers_with_width_cap():
    src = _src()
    assert "max-width:560px;margin:2px auto 4px;pointer-events:none;" in src
    # The old left-hugging margin is gone.
    assert "max-width:560px;margin:2px 0 4px;" not in src


def test_today_card_contrast_pins():
    src = _src()
    # Kicker "TODAY'S TRAINING" at --text2 (was --text3).
    assert ("color:var(--text2);margin-bottom:6px;\">TODAY'S TRAINING"
            in src)
    # Planned duration line at --text2.
    assert ('color:var(--text2);margin-bottom:8px;">'
            '${d.planned.duration_min}min') in src
    # Chip reason at --text2 (the named Planned→Now line stays --yellow).
    assert '${adjReason ? ` — <span style="color:var(--text2);">${esc(adjReason)}</span>` : \'\'}' in src
    # "Approximate shape" caption at --text2.
    assert 'color:var(--text2);margin-top:2px;">Approximate shape' in src
    # "Recovery details" header + planned-description subtitle read at
    # --text2 as well (pinned so they can't regress). 3.4.1 ⑩ turned the
    # <summary> fold into an always-visible header div.
    assert re.search(
        r'<div style="[^"]*color:var\(--text2\);[^"]*">Recovery details',
        src)
    assert ('color:var(--text2);margin-bottom:6px;">'
            '${esc(fixZeroMin(d.planned.description)') in src


def test_preview_chart_axis_labels_bumped_scoped_and_theme_safe():
    src = _src()
    # Scoped override: ONLY the today-card preview's --text3 axis/time
    # labels bump to --text2 (other consumers of the SVG builders keep
    # their look; brighter labels are untouched by the attribute selector).
    assert ('#today-blocks-preview svg text[fill="var(--text3)"] '
            '{ fill: var(--text2); }') in src
    # Both theme roots define --text2/--text3 so the bump resolves in each.
    dark = src[src.index(":root {"):src.index('[data-theme="light"]')]
    light = src[src.index('[data-theme="light"]'):]
    light = light[:light.index("}") + 1]
    for block, name in ((dark, "dark"), (light, "light")):
        assert "--text2:" in block, f"--text2 missing from {name} theme root"
        assert "--text3:" in block, f"--text3 missing from {name} theme root"


# ═══════════════════════════════════════════════════════════════════════════
# 4 — adjustment banner: reason once, no internal notation, clean Now-line
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_banner_renders_reason_exactly_once_no_notation():
    src = _src()
    # 3.4.1 M3: _todayPreviewSource consumes the shared _effectiveTodaySession
    # (one decision fn for card + day modal) — extract it too.
    fns = (_extract_js_function(src, "loadTodaySession")
           + _extract_js_function(src, "_effectiveTodaySession")
           + _extract_js_function(src, "_todayPreviewSource")
           + _extract_js_function(src, "_sessTypeLabel")
           + _extract_js_function(src, "_adjPlannedNowHtml")
           + _extract_js_function(src, "_todayPlannedZwoName"))
    harness = _ESC_STUB + """
// The screenshot case with the 3.4.1 engine copy (⑨b zone-accurate: the
// z6-dominant VO2max day reads "very hard riding", not "sprint intensity"):
// tempo demoted to z2 the day after a glycolytically heavy ride.
const REASON = 'Yesterday had 12 minutes of very hard riding (Z6/Z7) — easing today to Z2';
const payload = {
  planned:  { session_type: 'tempo', duration_min: 80, tss_estimate: 68,
              description: 'tempo (80min) — sampled from library',
              zwo_file: 'tempo_3x15.zwo' },
  adjusted: { session_type: 'z2', duration_min: 80, tss_estimate: 60,
              description: 'z2 (was tempo)' },
  was_modified: true,
  reason: REASON,
  adjustment_reason: REASON,
  readiness: 88,
  hr_target: {}, power_target: {},
  target_mode: 'power',
  retest_nudge: null,
};
const el = { innerHTML: '', style: {}, classList: { remove(){}, add(){} } };
const holder = { innerHTML: '' };
const els = { 'home-recommendation': el, 'today-blocks-preview': holder };
const $ = id => els[id] || null;
const document = { getElementById: id => els[id] || null };
const window = { _planData: { weeks: [{}] }, _athleteFtp: 250 };
const fetch = async () => ({ ok: true, json: async () => payload });
const fixZeroMin = s => s;
const _continuousSuggestionHtml = () => '';
const _deloadAdvanceChipHtml = () => '';
const buildPowerBlocks = () => [{ name: 'Main', min: 80, pctLow: 65, pctHigh: 65 }];
const renderPowerBlocksSVG = () => '<svg data-stub="synthetic"></svg>';
const workoutProfileSVG = () => '<svg data-stub="file"></svg>';
const renderRetestNudge = () => {};
const openTodayRich = () => {};
""" + fns + """
(async () => {
  await loadTodaySession();
  const html = el.innerHTML;
  if (!html) throw new Error('card never rendered');

  // The reason renders EXACTLY once (was three times pre-3.4.1).
  const n = html.split(REASON).length - 1;
  if (n !== 1) throw new Error('reason must render exactly once, got ' + n);

  // No internal notation anywhere user-visible.
  for (const bad of ['\\u22658', '\\u2265', 'Z6+Z7', 'glycolytically', 'dropped to']) {
    if (html.indexOf(bad) >= 0) throw new Error('notation leaked: ' + bad);
  }

  // Chip head + em-dash joiner (the "due to" scaffold read badly against a
  // full sentence and is gone). 3.4.2 M5 §1: the chip names BOTH workouts
  // (no fixture zwo_name/week cache here → label + duration name them).
  if (html.indexOf('Planned: <b>TEMPO, 80min</b>') < 0)
    throw new Error('chip must name the planned workout: ' + html);
  if (html.indexOf('Now: <b>Z2, 80min · 60 TSS</b>') < 0)
    throw new Error('chip must name the adjusted workout: ' + html);
  if (html.indexOf('Adjusted to <b>') >= 0)
    throw new Error('nameless chip lead must be gone');
  if (html.indexOf(' due to ') >= 0)
    throw new Error('"due to" joiner must be gone');

  // Contrast (item 3) on the LIVE render: kicker + duration + Now-line.
  if (html.indexOf(`color:var(--text2);margin-bottom:6px;">TODAY'S TRAINING`) < 0)
    throw new Error('kicker must render at --text2');
  if (html.indexOf('color:var(--text2);margin-bottom:8px;">80min · 68 TSS') < 0)
    throw new Error('duration line must render at --text2');
  if (html.indexOf('<span style="color:var(--text2);">' + REASON) < 0)
    throw new Error('chip reason must render at --text2');

  // Centered preview (item 2) on the LIVE render: the synthetic branch
  // filled the holder with the centered wrapper + the approximate caption.
  if (holder.innerHTML.indexOf('margin:2px auto 4px') < 0)
    throw new Error('preview wrapper must center (margin-inline auto)');
  if (holder.innerHTML.indexOf('max-width:560px') < 0)
    throw new Error('preview width cap must be preserved');
  if (holder.innerHTML.indexOf('Approximate shape') < 0)
    throw new Error('approximate caption missing from synthetic preview');
  if (holder.innerHTML.indexOf('color:var(--text2);margin-top:2px;">Approximate shape') < 0)
    throw new Error('approximate caption must render at --text2');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


def test_banner_template_reason_once_source_pin():
    """Belt-and-suspenders on the template itself: exactly one adjReason
    interpolation inside the banner chip, and the Now-line interpolates the
    description (which the engine now keeps reason-free)."""
    src = _src()
    fn = _extract_js_function(src, "loadTodaySession")
    assert fn.count("${esc(adjReason)}") == 1
    assert " due to " not in fn
    # 3.4.2 M5 §1: the chip line is the shared named builder.
    assert "${_adjPlannedNowHtml(d.planned, adj, _todayPlannedZwoName(d))}" in fn


# ═══════════════════════════════════════════════════════════════════════════
# 1b — HARDENED acceptance (owner escalation): the always-open card must
#      verifiably RENDER on the tab-open path, with zero stale wiring.
# ═══════════════════════════════════════════════════════════════════════════

def test_fatigue_ids_zero_stale_references():
    """Every id the loader/renderer touches exists exactly once in the
    markup, and no JS still references the retired <details> mechanics
    (panel .open force-open, ontoggle lazy-load)."""
    src = _src()
    for pid in ("fatigue-resistance-body", "fatigue-resistance-content",
                "fatigue-resistance-headline", "fatigue-resistance-panel",
                "fatigue-resistance-refresh-btn",
                "fatigue-resistance-info-popover"):
        assert src.count(f'id="{pid}"') == 1, f"{pid} must exist exactly once"
    # No surviving details-state accessors anywhere in the file.
    assert "fatigue-resistance-panel'.open" not in src
    assert "panel.open" not in _extract_js_function(
        src, "refreshFatigueResistance")
    assert src.count("loadFatigueResistance()") >= 1  # tab loader call
    assert "ontoggle=\"if(this.open)loadFatigueResistance" not in src


def _ancestor_stack_of(src: str, element_id: str):
    """Parse the template's static HTML and return the open-ancestor stack
    (tag, attrs) at the point `element_id` is declared. <script> bodies are
    CDATA to html.parser, so JS template literals can't pollute the walk."""
    from html.parser import HTMLParser

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    class Walker(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, dict]] = []
            self.found: list[tuple[str, dict]] | None = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if self.found is None and attrs.get("id") == element_id:
                self.found = list(self.stack) + [(tag, attrs)]
            if tag not in VOID:
                self.stack.append((tag, attrs))

        def handle_endtag(self, tag):
            for k in range(len(self.stack) - 1, -1, -1):
                if self.stack[k][0] == tag:
                    del self.stack[k:]
                    break

    w = Walker()
    w.feed(src)
    assert w.found is not None, f"#{element_id} not found in static markup"
    return w.found


def test_fatigue_card_visible_in_static_markup_no_hidden_ancestor():
    """The card must be renderable the moment the Analysis tab opens: no
    <details> ancestor, no inline display:none / hidden attribute anywhere
    up the chain, and it lives in #sec-analysis (whose visibility is the
    tab mechanism itself — loadAnalysisTab runs exactly when it opens)."""
    stack = _ancestor_stack_of(_src(), "fatigue-resistance-content")
    tags = [t for t, _ in stack]
    assert "details" not in tags, "fatigue content is inside a <details> fold"
    assert "summary" not in tags
    for tag, attrs in stack:
        style = (attrs.get("style") or "").replace(" ", "")
        assert "display:none" not in style, \
            f"hidden ancestor <{tag} style={attrs.get('style')!r}>"
        assert "hidden" not in attrs, f"[hidden] ancestor <{tag}>"
    section_ids = [a.get("id") for _, a in stack if a.get("id", "").startswith("sec-")]
    assert section_ids == ["sec-analysis"]


def test_home_recovery_details_visible_in_static_markup():
    """⑩ — same walk for the home snapshot: always visible inside #sec-home
    (statically .active), no fold, no hidden ancestor."""
    stack = _ancestor_stack_of(_src(), "home-snapshot-dfa")
    tags = [t for t, _ in stack]
    assert "details" not in tags
    for tag, attrs in stack:
        style = (attrs.get("style") or "").replace(" ", "")
        assert "display:none" not in style
        assert "hidden" not in attrs
    section = [a for _, a in stack if a.get("id") == "sec-home"]
    assert section and "active" in (section[0].get("class") or "")


@needs_node
def test_analysis_tab_open_renders_fatigue_content_without_click():
    """END-TO-END on the tab-open path: calling loadAnalysisTab() (exactly
    what the tab click runs) must drive loadFatigueResistance → fetch →
    renderFatigueResistance and WRITE the robustness result into
    #fatigue-resistance-body — no toggle, no extra click."""
    src = _src()
    fns = "\n".join(_extract_js_function(src, n) for n in (
        "loadAnalysisTab", "loadFatigueResistance", "renderFatigueResistance",
        "backfillBarHtml", "_fatigueClearPoll", "_fatigueSchedulePoll",
        "_fatigueUnfetchableNoteHtml", "_fatigueFooterBarHtml"))
    harness = _ESC_STUB + """
let _fatigueResistanceLoaded = false;
let _fatigueResistanceInflight = false;
let _fatigueResistanceThreshold = 1500;
let _fatigueResistanceWindowDays = 365;
let _fatigueResistanceBackfillStart = 0;
let _fatigueResistanceBackfillTimer = null;
const mk = () => {
  const el = { innerHTML: '', textContent: '', setAttribute() {},
               querySelector: () => null };
  return el;
};
const body = mk(), content = mk(), headline = mk();
const document = {
  querySelector: () => null,
  getElementById: id => ({
    'fatigue-resistance-body': body,
    'fatigue-resistance-content': content,
    'fatigue-resistance-headline': headline,
  }[id] || null),
};
const fetch = async (url) => {
  if (String(url).indexOf('/api/profile/fatigue-resistance') !== 0)
    throw new Error('unexpected fetch ' + url);
  return { ok: true, json: async () => ({
    fit_status: 'success', robustness_score: 87.5,
    n_long_rides: 14, n_long_rides_with_streams: 14, n_unfetchable: 0,
    power_streams_cached_pct: 100, auto_backfill_triggered: false,
    by_duration: [{duration_s: 60, fr_index_pct: 91.2, n_data_points: 9}],
    scatter: [] }) };
};
""" + fns + """
(async () => {
  loadAnalysisTab();          // ← the tab-open entry point, nothing else
  for (let i = 0; i < 200 && body.innerHTML.indexOf('Robustness') < 0; i++)
    await Promise.resolve();
  if (body.innerHTML.indexOf('Robustness 87.5%') < 0)
    throw new Error('tab open did not render fatigue content: ' + body.innerHTML);
  if (headline.textContent.indexOf('Robustness 87.5%') < 0)
    throw new Error('headline must carry the score');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)
