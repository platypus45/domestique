#!/usr/bin/env python3.12
"""Scrape whatsonzwift.com workout library via VISUAL-GRAPH INFERENCE.

IMPORTANT: This scraper NEVER hits any download/zwo/file endpoint on
whatsonzwift.com. It fetches only the public HTML detail page for each
workout and reconstructs the interval structure from the rendered
visual data:

  1. Parse the inline JSON / JS data array that feeds the SVG graph
     (preferred -- precise durations + % FTP).
  2. Parse <rect> elements of the SVG (width ~ duration, fill ~ power
     zone) as a fallback.
  3. Parse the textual interval table that sits alongside the graph
     ("3 min @ 55%, 5 x 30 sec @ 120% / 30 sec @ 55%, ...") as the
     last fallback.

Only the numeric interval data (unprotectable fact, Feist v. Rural) is
read. No <description> prose, <name>, <author>, <image>, <video>, or
<textevent> from the source is retained. Our own ZWO is authored with
<author>Domestique Library</author> and regenerated name/description.

Rate limit: 0.5s between requests. Honors HTTP 429 with exponential
backoff (5s -> 10s -> 20s -> 40s -> 60s cap, then skip URL).

Checkpoint: workouts/.scrape_progress.json tracks already-visited URLs
so re-running resumes without re-fetching.
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Local import (sibling script)
sys.path.insert(0, str(Path(__file__).parent))
from dedupe_zwo_library import structure_hash, load_index  # noqa: E402


BASE_URL = "https://whatsonzwift.com"
WORKOUTS_INDEX = f"{BASE_URL}/workouts"
USER_AGENT = "DomestiqueLibrary/4.0 (personal use)"
WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "workouts"
PROGRESS_FILE = WORKOUTS_DIR / ".scrape_progress.json"
MIN_REQUEST_INTERVAL_S = 0.5
REQUEST_TIMEOUT_S = 20
MAX_BACKOFF_S = 60


# ---- Type mapping (§3 of plan_scrape_strategy) ----
def _power_to_type(avg_power: float, max_power: float, on_dur: int) -> str:
    """Map interval power targets to our type slug."""
    if max_power >= 1.50 and on_dur <= 20:
        return "sprints"
    if max_power >= 1.20 and on_dur <= 60:
        return "anaerobic"
    if max_power >= 1.06:
        return "vo2"
    if max_power >= 0.95:
        return "threshold"
    if max_power >= 0.88:
        return "sweet_spot"
    if max_power >= 0.76:
        return "tempo"
    if max_power >= 0.56:
        return "endurance"
    return "recovery"


# ---- Checkpoint / progress ----
def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"visited_urls": [], "written_files": [], "dedupe_skips": []}


def _save_progress(p: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(p, indent=2))


# ---- Polite HTTP ----
_last_request_at = 0.0


def _polite_get(url: str) -> str | None:
    """GET url respecting rate-limit + 429 backoff. Returns body or None."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed + random.random() * 0.15)

    backoff = 5
    for attempt in range(5):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                _last_request_at = time.monotonic()
                body = resp.read().decode("utf-8", errors="replace")
                return body
        except urllib.error.HTTPError as e:
            _last_request_at = time.monotonic()
            if e.code == 429:
                print(f"  HTTP 429 on {url} -- backing off {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            if e.code in (404, 410):
                return None
            if 500 <= e.code < 600:
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            print(f"  HTTP {e.code} on {url} -- skipping", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            _last_request_at = time.monotonic()
            print(f"  network error on {url}: {e}; backoff {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 2)
    return None


# ---- HTML discovery ----
_HREF_ABS = re.compile(
    r'href="(https?://whatsonzwift\.com/workouts/[^"#?]+)"', re.IGNORECASE
)
_HREF_REL = re.compile(r'href="(/workouts/[^"#?]+)"', re.IGNORECASE)


def _all_hrefs(html_text: str) -> list[str]:
    """Extract all /workouts/... URLs (absolute + relative, normalized)."""
    out: list[str] = []
    for m in _HREF_ABS.finditer(html_text):
        href = m.group(1).rstrip("/")
        out.append(href)
    for m in _HREF_REL.finditer(html_text):
        href = m.group(1).rstrip("/")
        out.append(BASE_URL + href)
    return out


def discover_workout_urls(index_html: str) -> list[str]:
    """Find workout DETAIL URLs = /workouts/<collection>/<workout_slug>."""
    seen = set()
    urls = []
    for href in _all_hrefs(index_html):
        # Strip scheme+host -> /workouts/<...>
        try:
            path = href.split("whatsonzwift.com", 1)[1]
        except IndexError:
            continue
        parts = [p for p in path.strip("/").split("/") if p]
        # workout detail = /workouts/<collection>/<slug>
        if len(parts) != 3 or parts[0] != "workouts":
            continue
        full = BASE_URL + "/" + "/".join(parts)
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def discover_category_urls(index_html: str) -> list[str]:
    """Find collection index pages = /workouts/<collection>."""
    seen = set()
    urls = []
    for href in _all_hrefs(index_html):
        try:
            path = href.split("whatsonzwift.com", 1)[1]
        except IndexError:
            continue
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) != 2 or parts[0] != "workouts":
            continue
        full = BASE_URL + "/" + "/".join(parts)
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


# ---- Interval-data inference ----
@dataclass
class Segment:
    duration_sec: int
    power_low: float
    power_high: float
    kind: str = "SteadyState"  # SteadyState / Ramp / IntervalsT-child

    @property
    def avg_power(self) -> float:
        return (self.power_low + self.power_high) / 2.0


_INTERVAL_BLOCK_RE = re.compile(
    r"(\d+)\s*x\s*"
    r"(\d+(?:\.\d+)?)\s*(sec|s|min|m)\s*(?:@|at)\s*(\d+)\s*%"
    r".*?"
    r"(\d+(?:\.\d+)?)\s*(sec|s|min|m)\s*(?:@|at)\s*(\d+)\s*%",
    re.IGNORECASE | re.DOTALL,
)
_STEADY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(sec|s|min|m)\s*(?:@|at)\s*(\d+)\s*%(?:\s*(?:-|to)\s*(\d+)\s*%)?",
    re.IGNORECASE,
)


def _dur_to_sec(value: str, unit: str) -> int:
    v = float(value)
    if unit.lower().startswith("m") and unit.lower() != "ms":
        return int(round(v * 60))
    return int(round(v))


_TEXTBAR_RE = re.compile(
    r'<div class="textbar"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_DUR_RPM_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s*(min|sec|s|m)\b', re.IGNORECASE
)
_DATAVAL_RE = re.compile(
    r'data-value="(\d+(?:\.\d+)?)"\s+data-unit="relpow"', re.IGNORECASE
)


def parse_textbar_divs(page_html: str) -> list[Segment] | None:
    """Primary: parse <div class='textbar'>...</div> content.

    Each textbar has:
      - Leading "NNmin" / "NNsec" duration token
      - One (SteadyState) or two (Ramp) <span data-value=... data-unit="relpow">
    """
    out: list[Segment] = []
    for m in _TEXTBAR_RE.finditer(page_html):
        body = m.group(1)
        dm = _DUR_RPM_RE.match(body)
        if not dm:
            continue
        sec = _dur_to_sec(dm.group(1), dm.group(2))
        if sec < 1:
            continue
        vals = [float(v.group(1)) / 100.0 for v in _DATAVAL_RE.finditer(body)]
        if not vals:
            continue
        if len(vals) >= 2:
            lo, hi = vals[0], vals[1]
            kind = "Ramp" if abs(hi - lo) > 0.02 else "SteadyState"
            out.append(Segment(sec, lo, hi, kind))
        else:
            out.append(Segment(sec, vals[0], vals[0], "SteadyState"))
    if len(out) >= 2:
        return out
    return None


def parse_js_data_array(page_html: str) -> list[Segment] | None:
    """Look for inline JS data arrays feeding the graph.

    Pattern heuristic: arrays of objects like
    {Duration: 300, PowerLow: 0.5, PowerHigh: 0.65}
    or {d:300, p:0.65} etc.
    """
    # Match JSON-ish arrays with duration/power keys
    patt = re.compile(
        r"\[\s*\{[^}]*?(?:duration|Duration|dur)['\"]?\s*:\s*\d+[^\]]*?\]",
        re.IGNORECASE,
    )
    for m in patt.finditer(page_html):
        text = m.group(0)
        try:
            # Best-effort normalization: convert key names to quoted form
            fixed = re.sub(r"([\{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
            fixed = fixed.replace("'", '"')
            data = json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, list) or not data:
            continue
        segs: list[Segment] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            dur = (
                item.get("duration")
                or item.get("Duration")
                or item.get("dur")
                or item.get("d")
            )
            pl = (
                item.get("PowerLow")
                or item.get("powerLow")
                or item.get("power_low")
                or item.get("p")
            )
            ph = (
                item.get("PowerHigh")
                or item.get("powerHigh")
                or item.get("power_high")
                or pl
            )
            if dur is None or pl is None:
                continue
            try:
                ds = int(float(dur))
                pl_f = float(pl)
                ph_f = float(ph if ph is not None else pl)
            except (TypeError, ValueError):
                continue
            if pl_f > 3:
                pl_f /= 100.0
            if ph_f > 3:
                ph_f /= 100.0
            if ds < 1:
                continue
            kind = "Ramp" if abs(ph_f - pl_f) > 0.05 else "SteadyState"
            segs.append(Segment(ds, pl_f, ph_f, kind))
        if len(segs) >= 2:
            return segs
    return None


def parse_svg_rects(page_html: str) -> list[Segment] | None:
    """Fallback: parse <rect> width/height as (duration, power)."""
    # Quick filter -- must have a .workoutDetail-like SVG block
    svg_block_match = re.search(
        r"<svg[^>]*class=['\"][^'\"]*(graph|chart|workoutDetail)[^>]*>.*?</svg>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not svg_block_match:
        return None
    svg_block = svg_block_match.group(0)
    rect_patt = re.compile(
        r"<rect[^>]*?(?:x=['\"]([\d.]+)['\"])[^>]*?"
        r"(?:y=['\"]([\d.]+)['\"])[^>]*?"
        r"(?:width=['\"]([\d.]+)['\"])[^>]*?"
        r"(?:height=['\"]([\d.]+)['\"])",
        re.IGNORECASE,
    )
    rects = []
    for m in rect_patt.finditer(svg_block):
        try:
            rects.append(
                (
                    float(m.group(1)),
                    float(m.group(2)),
                    float(m.group(3)),
                    float(m.group(4)),
                )
            )
        except ValueError:
            continue
    if len(rects) < 2:
        return None
    rects.sort(key=lambda r: r[0])
    total_width = max((r[0] + r[2]) for r in rects) - min((r[0]) for r in rects)
    # We lack absolute scale without axis labels; cannot convert pixels
    # to seconds / % FTP safely. Return None and let text fallback run.
    return None


def parse_text_table(page_html: str) -> list[Segment] | None:
    """Fallback: parse the workout's textual interval list.

    Look for common patterns like:
      "5 min @ 55%"
      "4x (3 min @ 100%, 1 min @ 55%)"
      "Warm up 10 min from 50% to 70%"
    inside <li>, <tr>, or <p> near keywords like 'warm up' / 'cooldown'.
    """
    # Strip HTML tags to get rough text
    # (but preserve newlines between block elements)
    text = re.sub(r"<\s*(br|tr|li|p|div)[^>]*>", "\n", page_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    segs: list[Segment] = []
    for ln in lines:
        # "5 x 30 sec @ 120% / 30 sec @ 55%"
        m = _INTERVAL_BLOCK_RE.search(ln)
        if m:
            repeat = int(m.group(1))
            on_sec = _dur_to_sec(m.group(2), m.group(3))
            on_pct = int(m.group(4)) / 100.0
            off_sec = _dur_to_sec(m.group(5), m.group(6))
            off_pct = int(m.group(7)) / 100.0
            if 3 <= on_sec <= 7200 and 3 <= off_sec <= 7200 and 1 <= repeat <= 50:
                for _ in range(repeat):
                    segs.append(Segment(on_sec, on_pct, on_pct, "SteadyState"))
                    segs.append(Segment(off_sec, off_pct, off_pct, "SteadyState"))
                continue
        # "10 min from 50% to 70%" (warmup/cooldown/ramp)
        mr = re.search(
            r"(\d+(?:\.\d+)?)\s*(sec|s|min|m).*?(\d+)\s*%\s*(?:-|to)\s*(\d+)\s*%",
            ln,
            re.IGNORECASE,
        )
        if mr:
            sec = _dur_to_sec(mr.group(1), mr.group(2))
            lo = int(mr.group(3)) / 100.0
            hi = int(mr.group(4)) / 100.0
            if 10 <= sec <= 7200:
                segs.append(Segment(sec, lo, hi, "Ramp"))
                continue
        # "5 min @ 65%"
        ms = _STEADY_RE.search(ln)
        if ms:
            sec = _dur_to_sec(ms.group(1), ms.group(2))
            pct = int(ms.group(3)) / 100.0
            if 10 <= sec <= 7200 and 0.2 <= pct <= 2.0:
                segs.append(Segment(sec, pct, pct, "SteadyState"))

    if len(segs) >= 2:
        # Sanity: total duration plausible (5-120 min)
        total = sum(s.duration_sec for s in segs)
        if 300 <= total <= 7200:
            return segs
    return None


def infer_segments(page_html: str) -> list[Segment] | None:
    for fn in (parse_textbar_divs, parse_js_data_array, parse_svg_rects, parse_text_table):
        segs = fn(page_html)
        if segs and len(segs) >= 2:
            return segs
    return None


# ---- ZWO emission ----
def _fmt_pw(p: float) -> str:
    return f"{p:.2f}".rstrip("0").rstrip(".") or "0"


def _segment_xml(seg: Segment) -> str:
    if seg.kind == "Ramp" or abs(seg.power_high - seg.power_low) > 0.02:
        if seg.avg_power > 0.75:
            # likely warmup/cooldown detection happens in emit_zwo
            pass
        return (
            f'        <SteadyState Duration="{seg.duration_sec}" '
            f'Power="{_fmt_pw(seg.avg_power)}" pace="0" />\n'
        )
    return (
        f'        <SteadyState Duration="{seg.duration_sec}" '
        f'Power="{_fmt_pw(seg.power_low)}" pace="0" />\n'
    )


def _collapse_and_classify(segs: list[Segment]) -> tuple[list[Segment], str, int]:
    """Return (segments, type_slug, total_min)."""
    total_sec = sum(s.duration_sec for s in segs)
    total_min = int(round(total_sec / 60))
    if not segs:
        return segs, "endurance", total_min
    # Max power target from "work" segments (exclude warmup/cooldown extremes)
    work_segs = segs[1:-1] if len(segs) >= 3 else segs
    max_p = max((s.power_high for s in work_segs), default=segs[0].power_high)
    # Shortest "high" on_dur (for sprint classification)
    high_on = min(
        (s.duration_sec for s in work_segs if s.avg_power >= 1.0),
        default=60,
    )
    # Mean of non-warmup/cooldown
    avg_p = sum(s.avg_power * s.duration_sec for s in work_segs) / max(
        1, sum(s.duration_sec for s in work_segs)
    )
    slug = _power_to_type(avg_p, max_p, high_on)
    # Over-under heuristic: alternating above/below FTP
    if len(work_segs) >= 4:
        ups = sum(1 for s in work_segs if s.avg_power >= 1.0)
        downs = sum(1 for s in work_segs if 0.7 <= s.avg_power < 1.0)
        if ups >= 2 and downs >= 2 and abs(ups - downs) <= 2:
            slug = "over_under"
    # Pyramid heuristic: monotonic then reverse
    if len(work_segs) >= 5:
        powers = [round(s.avg_power, 2) for s in work_segs if s.duration_sec >= 60]
        if len(powers) >= 5:
            mid = len(powers) // 2
            if powers[: mid + 1] == sorted(powers[: mid + 1]) and powers[mid:] == sorted(
                powers[mid:], reverse=True
            ):
                slug = "pyramid"
    return segs, slug, total_min


def _build_description(segs: list[Segment], slug: str, total_min: int) -> str:
    """Regenerate neutral description from structure (no scraped prose)."""
    parts = []
    # Intervals collapse: count consecutive (on, off) pairs with same power
    i = 0
    n = len(segs)
    while i < n:
        s = segs[i]
        # Look-ahead to find repeating pattern
        pair_len = 0
        repeats = 0
        if i + 1 < n:
            on = segs[i]
            off = segs[i + 1]
            j = i
            while (
                j + 1 < n
                and abs(segs[j].duration_sec - on.duration_sec) <= 2
                and abs(segs[j + 1].duration_sec - off.duration_sec) <= 2
                and abs(segs[j].avg_power - on.avg_power) < 0.02
                and abs(segs[j + 1].avg_power - off.avg_power) < 0.02
            ):
                j += 2
                repeats += 1
            if repeats >= 2:
                parts.append(
                    f"{repeats} x {_fmt_time(on.duration_sec)} @ {int(on.avg_power*100)}% FTP / "
                    f"{_fmt_time(off.duration_sec)} @ {int(off.avg_power*100)}% FTP"
                )
                i += repeats * 2
                continue
        # Lone segment
        if s.kind == "Ramp" or abs(s.power_high - s.power_low) > 0.05:
            parts.append(
                f"{_fmt_time(s.duration_sec)} ramp {int(s.power_low*100)}-{int(s.power_high*100)}% FTP"
            )
        else:
            parts.append(f"{_fmt_time(s.duration_sec)} @ {int(s.avg_power*100)}% FTP")
        i += 1
    parts.append(f"Total {total_min} min")
    return " | ".join(parts)


def _fmt_time(sec: int) -> str:
    if sec >= 60:
        m = sec / 60
        if abs(m - round(m)) < 0.01:
            return f"{int(round(m))} min"
        return f"{m:.1f} min"
    return f"{sec} sec"


def _build_name(slug: str, total_min: int, segs: list[Segment]) -> str:
    # Try to describe the dominant interval count
    # Find longest repeated (on,off) pair
    i = 0
    best = None
    while i + 1 < len(segs):
        on = segs[i]
        off = segs[i + 1]
        j = i
        reps = 0
        while (
            j + 1 < len(segs)
            and abs(segs[j].duration_sec - on.duration_sec) <= 2
            and abs(segs[j + 1].duration_sec - off.duration_sec) <= 2
        ):
            j += 2
            reps += 1
        if reps >= 2 and (best is None or reps > best[0]):
            best = (reps, on.duration_sec)
        i = max(i + 1, j)
    label = {
        "recovery": "Recovery",
        "endurance": "Endurance",
        "tempo": "Tempo",
        "sweet_spot": "Sweet Spot",
        "threshold": "Threshold",
        "vo2": "VO2max",
        "anaerobic": "Anaerobic",
        "sprints": "Sprints",
        "over_under": "Over-Under",
        "pyramid": "Pyramid",
    }.get(slug, slug.replace("_", " ").title())
    if best:
        reps, on_sec = best
        return f"{label} {reps}x{_fmt_time(on_sec).replace(' ', '')} ({total_min}min)"
    return f"{label} ({total_min}min)"


def emit_zwo(segs: list[Segment]) -> tuple[str, str, int]:
    """Return (zwo_xml_text, type_slug, total_min)."""
    segs, slug, total_min = _collapse_and_classify(segs)
    body = ""
    # First segment as Warmup if it's a low-power ramp, last as Cooldown similarly.
    if segs:
        first = segs[0]
        if first.kind == "Ramp" or (
            abs(first.power_high - first.power_low) > 0.05 and first.avg_power < 0.8
        ):
            body += (
                f'        <Warmup Duration="{first.duration_sec}" '
                f'PowerLow="{_fmt_pw(first.power_low)}" '
                f'PowerHigh="{_fmt_pw(first.power_high)}" pace="0" />\n'
            )
            mid_segs = segs[1:]
        else:
            mid_segs = segs
        # Detect final cooldown
        last = mid_segs[-1] if mid_segs else None
        if last and (
            last.kind == "Ramp"
            or (abs(last.power_high - last.power_low) > 0.05 and last.avg_power < 0.75)
        ):
            mids = mid_segs[:-1]
            cooldown = last
        else:
            mids = mid_segs
            cooldown = None
        for s in mids:
            if abs(s.power_high - s.power_low) > 0.05:
                body += (
                    f'        <Ramp Duration="{s.duration_sec}" '
                    f'PowerLow="{_fmt_pw(s.power_low)}" '
                    f'PowerHigh="{_fmt_pw(s.power_high)}" pace="0" />\n'
                )
            else:
                body += (
                    f'        <SteadyState Duration="{s.duration_sec}" '
                    f'Power="{_fmt_pw(s.power_low)}" pace="0" />\n'
                )
        if cooldown:
            body += (
                f'        <Cooldown Duration="{cooldown.duration_sec}" '
                f'PowerLow="{_fmt_pw(cooldown.power_high)}" '
                f'PowerHigh="{_fmt_pw(cooldown.power_low)}" pace="0" />\n'
            )
    name = _build_name(slug, total_min, segs)
    desc = _build_description(segs, slug, total_min)
    xml = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workout_file>\n"
        "    <author>Domestique Library</author>\n"
        f"    <name>{html.escape(name)}</name>\n"
        f"    <description>{html.escape(desc)}</description>\n"
        "    <sportType>bike</sportType>\n"
        "    <workout>\n"
        f"{body}"
        "    </workout>\n"
        "</workout_file>\n"
    )
    return xml, slug, total_min


def _structure_sig(segs: list[Segment]) -> str:
    """Compact signature for filenames."""
    # Largest repeating (on,off) pair
    i = 0
    best = None
    while i + 1 < len(segs):
        on = segs[i]
        off = segs[i + 1]
        j = i
        reps = 0
        while (
            j + 1 < len(segs)
            and abs(segs[j].duration_sec - on.duration_sec) <= 2
            and abs(segs[j + 1].duration_sec - off.duration_sec) <= 2
        ):
            j += 2
            reps += 1
        if reps >= 2 and (best is None or reps > best[0]):
            best = (reps, on.duration_sec)
        i = max(i + 1, j)
    if best:
        reps, on_sec = best
        if on_sec >= 60:
            return f"{reps}x{int(round(on_sec/60))}min"
        return f"{reps}x{on_sec}s"
    return "steady"


def filename_for(segs: list[Segment], slug: str, total_min: int) -> str:
    sig = _structure_sig(segs)
    return f"{slug}_{sig}_{total_min}min.zwo"


# ---- Main scrape loop ----
def scrape(
    max_duration_min: int,
    wallclock_budget_s: int,
    max_pages: int | None,
) -> dict:
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    progress = _load_progress()
    index = load_index(WORKOUTS_DIR)
    visited = set(progress["visited_urls"])
    written = list(progress["written_files"])
    skips = list(progress["dedupe_skips"])

    stats = {
        "pages_crawled": 0,
        "workouts_inferred": 0,
        "workouts_written": 0,
        "dedupe_skips": 0,
        "parse_failures": 0,
        "over_max_duration": 0,
        "under_minimum": 0,
        "http_failures": 0,
    }

    # Fetch top-level index
    print("Fetching workouts index...", file=sys.stderr)
    started = time.monotonic()
    root_html = _polite_get(WORKOUTS_INDEX)
    stats["pages_crawled"] += 1
    if root_html is None:
        print("Could not fetch workouts index. Aborting scrape.", file=sys.stderr)
        return stats

    # Discover category pages + direct workout pages from index
    candidate_urls: list[str] = []
    candidate_urls.extend(discover_workout_urls(root_html))
    for cat_url in discover_category_urls(root_html):
        if time.monotonic() - started > wallclock_budget_s:
            break
        if cat_url in visited:
            continue
        print(f"  cat: {cat_url}", file=sys.stderr)
        body = _polite_get(cat_url)
        stats["pages_crawled"] += 1
        visited.add(cat_url)
        if body is None:
            stats["http_failures"] += 1
            continue
        candidate_urls.extend(discover_workout_urls(body))

    # De-dup
    seen = set()
    ordered = []
    for u in candidate_urls:
        if u in seen:
            continue
        seen.add(u)
        ordered.append(u)

    print(
        f"Discovered {len(ordered)} candidate workout URLs.",
        file=sys.stderr,
    )

    for url in ordered:
        if time.monotonic() - started > wallclock_budget_s:
            print("Wall-clock budget exhausted; checkpointing.", file=sys.stderr)
            break
        if max_pages and stats["pages_crawled"] >= max_pages:
            break
        if url in visited:
            continue

        body = _polite_get(url)
        stats["pages_crawled"] += 1
        visited.add(url)
        if body is None:
            stats["http_failures"] += 1
            continue

        segs = infer_segments(body)
        if not segs:
            stats["parse_failures"] += 1
            continue
        stats["workouts_inferred"] += 1

        total_min = int(round(sum(s.duration_sec for s in segs) / 60))
        if total_min < 5:
            stats["under_minimum"] += 1
            continue
        if total_min > max_duration_min:
            stats["over_max_duration"] += 1
            continue

        zwo_text, slug, total_min = emit_zwo(segs)

        # Write to a temp path, compute structure hash, then rename or discard
        fname = filename_for(segs, slug, total_min)
        tmp = WORKOUTS_DIR / f".tmp_{fname}"
        tmp.write_text(zwo_text)
        h = structure_hash(tmp)
        if h in index:
            existing = index[h]
            stats["dedupe_skips"] += 1
            skips.append({"source_url": url, "matches": existing})
            print(
                f"DEDUPE: {url} matches existing {existing}",
                file=sys.stderr,
            )
            tmp.unlink(missing_ok=True)
            # Persist progress periodically
            if (stats["pages_crawled"] % 10) == 0:
                progress.update(
                    {
                        "visited_urls": sorted(visited),
                        "written_files": written,
                        "dedupe_skips": skips,
                    }
                )
                _save_progress(progress)
            continue

        # Resolve filename collision (different structure, same name)
        final_path = WORKOUTS_DIR / fname
        if final_path.exists():
            v = 2
            while (WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo").exists():
                v += 1
            final_path = WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo"
        tmp.rename(final_path)
        index[h] = final_path.name
        written.append({"source_url": url, "file": final_path.name, "hash": h})
        stats["workouts_written"] += 1
        print(f"  wrote {final_path.name} <- {url}", file=sys.stderr)

        # Persist progress periodically
        if (stats["workouts_written"] % 10) == 0:
            progress.update(
                {
                    "visited_urls": sorted(visited),
                    "written_files": written,
                    "dedupe_skips": skips,
                }
            )
            _save_progress(progress)

    # Final checkpoint
    progress.update(
        {
            "visited_urls": sorted(visited),
            "written_files": written,
            "dedupe_skips": skips,
        }
    )
    _save_progress(progress)

    # Refresh structure index on disk
    idx_path = WORKOUTS_DIR / ".structure_index.json"
    idx_path.write_text(json.dumps(index, indent=2, sort_keys=True))

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-duration", type=int, default=60, help="Max workout length (min)")
    ap.add_argument(
        "--budget-seconds",
        type=int,
        default=45 * 60,
        help="Wall-clock budget (default 45 min)",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap on pages fetched (test only)",
    )
    args = ap.parse_args()
    stats = scrape(
        max_duration_min=args.max_duration,
        wallclock_budget_s=args.budget_seconds,
        max_pages=args.max_pages,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
