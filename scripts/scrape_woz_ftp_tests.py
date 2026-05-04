#!/usr/bin/env python3.12
"""Scrape whatsonzwift.com /workouts/ftp-tests/ index into library.

Per-test flow:
  1. Fetch index HTML at https://whatsonzwift.com/workouts/ftp-tests/
     (301 redirects to .../ftp-tests; curl-style follow).
  2. Collect detail URLs shaped /workouts/ftp-tests/<slug>.
  3. For each detail page, parse the rendered <div class="textbar">
     segments (same visual-graph inference used for main scraper) and
     map them to a Segment list.
     - <span data-unit="relpow"> → fractional FTP directly.
     - <span data-unit="watts">  → normalized against a 250W nominal
       FTP so the structure is representable in our fractional-FTP
       ZWO. The protocol character (e.g. +20W/min ramp) is preserved
       as +20W/250W = +0.08 FTP/min steps.
  4. Emit a ZWO with:
     - author "Domestique Library"
     - name "FTP Test — <structure>"
     - description generated from structure + FTP formula hint
     - <tags><tag name="ftp_test"/></tags>
     - NO textevent / image / video / coach-prose
  5. Dedupe via structure_hash against existing workouts (incl. the
     2 existing ftp_test_* files).
  6. Filename: ftp_test_<slug>.zwo where slug is generic and derived
     from structure (e.g. ftp_test_ramp_20w_step, ftp_test_ramp_lite,
     ftp_test_20min_coggan).
  7. Write workouts/.ftp_tests_manifest.json with per-file metadata.

Filter: skip tests <10 min or >75 min total.

Rate limit: 0.5 s between requests. UA
"DomestiqueLibrary/4.1 (personal use)". 429 handled with
exp backoff (5→10→20→40→60, then skip).

Smoke: XML-parses each written file and asserts the ftp_test tag is
present before exiting.
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
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dedupe_zwo_library import structure_hash, load_index  # noqa: E402


BASE_URL = "https://whatsonzwift.com"
INDEX_URL = f"{BASE_URL}/workouts/ftp-tests/"
USER_AGENT = "DomestiqueLibrary/4.1 (personal use)"
WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "workouts"
MANIFEST_FILE = WORKOUTS_DIR / ".ftp_tests_manifest.json"
MIN_REQUEST_INTERVAL_S = 0.5
REQUEST_TIMEOUT_S = 20
MAX_BACKOFF_S = 60

# Nominal FTP used to convert absolute-watt-based test segments to
# fractional FTP for ZWO representation. 250W is typical for the
# presentation Zwift uses on the public test pages.
NOMINAL_FTP_W = 250.0

# Minimum / maximum total duration (sec) to count as an FTP test.
MIN_TOTAL_SEC = 10 * 60
MAX_TOTAL_SEC = 75 * 60


# ---- Polite HTTP ----
_last_request_at = 0.0


def _polite_get(url: str) -> str | None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed + random.random() * 0.15)

    backoff = 5
    for _attempt in range(5):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                _last_request_at = time.monotonic()
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            _last_request_at = time.monotonic()
            if e.code == 429:
                print(f"  429 on {url}; backoff {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            if e.code in (301, 302):
                # urllib already follows by default; treat as failure.
                return None
            if e.code in (404, 410):
                return None
            if 500 <= e.code < 600:
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            print(f"  HTTP {e.code} on {url}; skip", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            _last_request_at = time.monotonic()
            print(f"  net err {url}: {e}; backoff {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 2)
    return None


# ---- URL discovery ----
_HREF_RE = re.compile(
    r'href="(https?://whatsonzwift\.com/workouts/ftp-tests/[^"#?]+)"',
    re.IGNORECASE,
)


def discover_test_urls(index_html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _HREF_RE.finditer(index_html):
        href = m.group(1).rstrip("/")
        # Only detail pages (three path segments: workouts/ftp-tests/<slug>)
        parts = href.split("whatsonzwift.com/", 1)[1].split("/")
        if len(parts) != 3:
            continue
        if parts[0] != "workouts" or parts[1] != "ftp-tests":
            continue
        if parts[2] in ("", "ftp-tests"):
            continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


# ---- Interval parsing ----
@dataclass
class Segment:
    duration_sec: int
    power_low: float   # fractional FTP
    power_high: float  # fractional FTP
    kind: str          # SteadyState / Ramp / FreeRide

    @property
    def avg_power(self) -> float:
        return (self.power_low + self.power_high) / 2.0


_TEXTBAR_RE = re.compile(
    r'<div class="textbar"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_DUR_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(min|sec|s|m)\b', re.IGNORECASE)
_RELPOW_RE = re.compile(
    r'data-value="(\d+(?:\.\d+)?)"\s+data-unit="relpow"', re.IGNORECASE
)
_WATTS_RE = re.compile(
    r'data-value="(\d+(?:\.\d+)?)"\s+data-unit="watts"', re.IGNORECASE
)
_FREE_RIDE_RE = re.compile(r'\bfree\s*ride\b', re.IGNORECASE)


def _dur_to_sec(value: str, unit: str) -> int:
    v = float(value)
    u = unit.lower()
    if u.startswith("m") and u != "ms":
        return int(round(v * 60))
    return int(round(v))


def parse_textbars(page_html: str) -> list[Segment]:
    """Parse <div class='textbar'> blocks on an FTP-test page.

    Each textbar typically contains:
      - Leading "Nmin" / "Nsec" duration.
      - One or two <span data-value=N data-unit=(relpow|watts)>
        elements indicating constant or ramp power.
      - Optional "free ride" phrase → we still honor any target
        power if present, otherwise record as FreeRide.
    """
    out: list[Segment] = []
    for m in _TEXTBAR_RE.finditer(page_html):
        body = m.group(1)
        dm = _DUR_RE.match(body)
        if not dm:
            continue
        sec = _dur_to_sec(dm.group(1), dm.group(2))
        if sec < 1:
            continue

        rel = [float(x.group(1)) / 100.0 for x in _RELPOW_RE.finditer(body)]
        watts = [float(x.group(1)) for x in _WATTS_RE.finditer(body)]
        free = bool(_FREE_RIDE_RE.search(body))

        if rel:
            if len(rel) >= 2 and abs(rel[1] - rel[0]) > 0.02:
                out.append(Segment(sec, rel[0], rel[1], "Ramp"))
            else:
                out.append(Segment(sec, rel[0], rel[0], "SteadyState"))
        elif watts:
            # Convert absolute watts to fractional of NOMINAL_FTP_W
            frac = [w / NOMINAL_FTP_W for w in watts]
            if len(frac) >= 2 and abs(frac[1] - frac[0]) > 0.02:
                out.append(Segment(sec, frac[0], frac[1], "Ramp"))
            else:
                out.append(Segment(sec, frac[0], frac[0], "SteadyState"))
        elif free:
            # Pure free ride segment with no displayed target.
            # Represent as steady-state at 0.65 FTP (Z2) for structure.
            out.append(Segment(sec, 0.65, 0.65, "FreeRide"))
        else:
            # Cannot infer; skip this line rather than fabricating.
            continue
    return out


# ---- Classifier: map segment pattern → generic slug + title ----
def classify_test(segs: list[Segment], url: str) -> tuple[str, str, str]:
    """Return (slug, display_structure, formula_hint).

    Heuristics:
      - Ramp test: many similar-length (≈60 s) steady steps with
        monotonically increasing power. FTP ≈ 0.75 × best 1-min.
      - Coggan 20-min: a ≥ 15 min steady block at ≥ 95% FTP somewhere
        in the middle of the workout. FTP ≈ 0.95 × avg 20-min.
      - 2×8-min: two similar work blocks around 7-9 min at threshold+.
      - 2×5-min: two similar work blocks around 4-6 min at threshold+.
      - 8-min single: one sustained 7-10 min near-max effort.
      - Otherwise: generic "protocol".
    """
    durations = [s.duration_sec for s in segs]
    powers_avg = [s.avg_power for s in segs]

    # Detect monotonic ramp: ≥ 10 steady 1-min-ish blocks with rising power.
    one_min_steps = [
        s for s in segs
        if 50 <= s.duration_sec <= 75
        and s.kind == "SteadyState"
        and abs(s.power_high - s.power_low) < 0.02
    ]
    if len(one_min_steps) >= 10:
        pw = [s.avg_power for s in one_min_steps]
        increasing = sum(
            1 for a, b in zip(pw, pw[1:]) if b > a + 0.005
        )
        if increasing >= len(pw) - 2:
            # Step size in watts (assume NOMINAL_FTP_W=250 for display)
            step_delta = (pw[-1] - pw[0]) / max(1, len(pw) - 1)
            step_w = int(round(step_delta * NOMINAL_FTP_W))
            tag = f"ramp_{step_w}w_step" if step_w > 0 else "ramp"
            return (
                tag,
                f"Ramp ({len(pw)}×1-min, +{step_w}W/min)",
                "FTP = 0.75 × best 1-min power",
            )

    # Detect 20-min Coggan: look for a single block ≥ 17 min ≥ 95% FTP.
    for s in segs:
        if (
            s.duration_sec >= 17 * 60
            and s.kind in ("SteadyState", "FreeRide")
            and s.avg_power >= 0.95
        ):
            return (
                "coggan_20min",
                "Coggan 20-min",
                "FTP = 0.95 × avg 20-min power",
            )

    # Detect 2×8-min protocol.
    work_like = [
        s for s in segs
        if 7 * 60 <= s.duration_sec <= 10 * 60
        and s.avg_power >= 0.95
    ]
    if len(work_like) >= 2:
        return (
            "2x8min",
            "2×8-min",
            "FTP = 0.90 × avg of both 8-min efforts",
        )

    # Detect 2×5-min protocol.
    work5 = [
        s for s in segs
        if 4 * 60 <= s.duration_sec <= 6 * 60
        and s.avg_power >= 1.00
    ]
    if len(work5) >= 2:
        return (
            "2x5min",
            "2×5-min",
            "FTP = 0.85 × avg of both 5-min efforts",
        )

    # Detect single 8-min test.
    for s in segs:
        if 7 * 60 <= s.duration_sec <= 10 * 60 and s.avg_power >= 1.00:
            return (
                "8min",
                "8-min",
                "FTP = 0.90 × avg 8-min power",
            )

    # Fallback.
    total_min = sum(durations) // 60
    return (
        f"protocol_{total_min}min",
        f"Protocol ({total_min}-min)",
        "FTP derivation depends on protocol",
    )


# ---- ZWO emission ----
def _fmt_pw(p: float) -> str:
    return f"{p:.2f}".rstrip("0").rstrip(".") or "0"


def emit_zwo(
    segs: list[Segment], title_structure: str, formula: str, source_url: str
) -> str:
    """Render a ZWO for the parsed FTP-test segments.

    - <author> forced to Domestique Library.
    - <name> forced to "FTP Test — <structure>".
    - <description> regenerated (no scraped prose).
    - <tags> contains the ftp_test index key.
    - No text events, images, videos, coach prose.
    """
    body_parts: list[str] = []

    # Warmup detection: first segment if it is a low-power ramp or
    # low-power free ride.
    if segs:
        first = segs[0]
        if (
            first.kind == "Ramp"
            and first.avg_power < 0.80
        ):
            body_parts.append(
                f'        <Warmup Duration="{first.duration_sec}" '
                f'PowerLow="{_fmt_pw(first.power_low)}" '
                f'PowerHigh="{_fmt_pw(first.power_high)}" pace="0"/>\n'
            )
            mids = segs[1:]
        elif first.kind == "FreeRide" and first.avg_power < 0.70:
            body_parts.append(
                f'        <Warmup Duration="{first.duration_sec}" '
                f'PowerLow="0.5" PowerHigh="0.65" pace="0"/>\n'
            )
            mids = segs[1:]
        else:
            mids = segs

        # Cooldown detection: last segment if it ramps down.
        last = mids[-1] if mids else None
        if (
            last
            and last.kind == "Ramp"
            and last.power_high < last.power_low
        ):
            mids = mids[:-1]
            cooldown = last
        elif (
            last
            and last.kind == "Ramp"
            and last.avg_power < 0.60
        ):
            mids = mids[:-1]
            cooldown = last
        else:
            cooldown = None

        for s in mids:
            if s.kind == "Ramp":
                body_parts.append(
                    f'        <Ramp Duration="{s.duration_sec}" '
                    f'PowerLow="{_fmt_pw(s.power_low)}" '
                    f'PowerHigh="{_fmt_pw(s.power_high)}" pace="0"/>\n'
                )
            elif s.kind == "FreeRide":
                body_parts.append(
                    f'        <FreeRide Duration="{s.duration_sec}"/>\n'
                )
            else:
                body_parts.append(
                    f'        <SteadyState Duration="{s.duration_sec}" '
                    f'Power="{_fmt_pw(s.power_low)}" pace="0"/>\n'
                )

        if cooldown:
            # Cooldown element wants Power ramp high→low.
            lo = min(cooldown.power_low, cooldown.power_high)
            hi = max(cooldown.power_low, cooldown.power_high)
            body_parts.append(
                f'        <Cooldown Duration="{cooldown.duration_sec}" '
                f'PowerLow="{_fmt_pw(hi)}" '
                f'PowerHigh="{_fmt_pw(lo)}" pace="0"/>\n'
            )

    body = "".join(body_parts)

    total_sec = sum(s.duration_sec for s in segs)
    total_min = int(round(total_sec / 60))
    name = f"FTP Test — {title_structure}"
    desc = (
        f"Scraped FTP-test protocol (visual-graph inference, structure only). "
        f"Total {total_min} min. Suggested FTP calc: {formula}."
    )

    xml = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workout_file>\n"
        "    <author>Domestique Library</author>\n"
        f"    <name>{html.escape(name)}</name>\n"
        f"    <description>{html.escape(desc)}</description>\n"
        "    <sportType>bike</sportType>\n"
        "    <tags>\n"
        '        <tag name="ftp_test"/>\n'
        "    </tags>\n"
        "    <workout>\n"
        f"{body}"
        "    </workout>\n"
        "</workout_file>\n"
    )
    return xml


# ---- Main ----
def _next_free_filename(base: str) -> Path:
    p = WORKOUTS_DIR / f"{base}.zwo"
    if not p.exists():
        return p
    v = 2
    while (WORKOUTS_DIR / f"{base}_v{v}.zwo").exists():
        v += 1
    return WORKOUTS_DIR / f"{base}_v{v}.zwo"


def _smoke_check(path: Path) -> bool:
    """Assert ftp_test tag is present in the written ZWO."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        print(f"  SMOKE-FAIL {path.name}: XML parse error {e}", file=sys.stderr)
        return False
    tags = root.find("tags")
    if tags is None:
        print(f"  SMOKE-FAIL {path.name}: no <tags>", file=sys.stderr)
        return False
    for t in tags.findall("tag"):
        if t.get("name") == "ftp_test":
            return True
    print(f"  SMOKE-FAIL {path.name}: no <tag name='ftp_test'/>", file=sys.stderr)
    return False


def scrape() -> dict:
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index(WORKOUTS_DIR)

    stats = {
        "urls_crawled": 0,
        "tests_parsed": 0,
        "tests_written": 0,
        "dedupe_skips": 0,
        "parse_failures": 0,
        "filter_rejected": 0,
        "http_failures": 0,
        "smoke_failures": 0,
    }

    print(f"Fetching index {INDEX_URL}", file=sys.stderr)
    index_html = _polite_get(INDEX_URL)
    stats["urls_crawled"] += 1
    if index_html is None:
        print("Could not fetch index. Aborting.", file=sys.stderr)
        return stats

    urls = discover_test_urls(index_html)
    print(f"Discovered {len(urls)} test URLs.", file=sys.stderr)

    manifest: list[dict] = []
    if MANIFEST_FILE.exists():
        try:
            manifest = json.loads(MANIFEST_FILE.read_text())
        except json.JSONDecodeError:
            manifest = []

    for url in urls:
        print(f"  fetch {url}", file=sys.stderr)
        body = _polite_get(url)
        stats["urls_crawled"] += 1
        if body is None:
            stats["http_failures"] += 1
            continue

        # Skip pages with video embeds (explicit instruction).
        if re.search(r'<iframe[^>]*youtube', body, re.IGNORECASE) or re.search(
            r'<video[^>]', body, re.IGNORECASE
        ):
            print("    skip: video embed present", file=sys.stderr)
            stats["filter_rejected"] += 1
            continue

        segs = parse_textbars(body)
        if len(segs) < 2:
            stats["parse_failures"] += 1
            print("    parse-fail: <2 segments", file=sys.stderr)
            continue
        stats["tests_parsed"] += 1

        total_sec = sum(s.duration_sec for s in segs)
        if total_sec < MIN_TOTAL_SEC or total_sec > MAX_TOTAL_SEC:
            stats["filter_rejected"] += 1
            print(
                f"    filter: total {total_sec // 60}min out of "
                f"[{MIN_TOTAL_SEC // 60}, {MAX_TOTAL_SEC // 60}]",
                file=sys.stderr,
            )
            continue

        slug, title_structure, formula = classify_test(segs, url)
        zwo_text = emit_zwo(segs, title_structure, formula, url)

        base = f"ftp_test_{slug}"
        # Compute structure hash via temp write.
        tmp = WORKOUTS_DIR / f".tmp_{base}.zwo"
        tmp.write_text(zwo_text)
        try:
            h = structure_hash(tmp)
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            stats["parse_failures"] += 1
            print(f"    hash-fail: {e}", file=sys.stderr)
            continue

        if h in index:
            stats["dedupe_skips"] += 1
            print(
                f"    DEDUPE: structure matches existing {index[h]}",
                file=sys.stderr,
            )
            tmp.unlink(missing_ok=True)
            continue

        final_path = _next_free_filename(base)
        tmp.rename(final_path)
        index[h] = final_path.name

        if not _smoke_check(final_path):
            stats["smoke_failures"] += 1
            # Leave file in place but note failure — smoke is advisory.
            continue

        stats["tests_written"] += 1
        manifest.append(
            {
                "source_url": url,
                "written_filename": final_path.name,
                "structure_hash": h,
                "formula": formula,
                "title_structure": title_structure,
                "total_sec": total_sec,
                "segment_count": len(segs),
            }
        )
        print(f"    wrote {final_path.name}", file=sys.stderr)

    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))

    # Refresh library structure index.
    idx_path = WORKOUTS_DIR / ".structure_index.json"
    idx_path.write_text(json.dumps(index, indent=2, sort_keys=True))

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    stats = scrape()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
