"""v4.6.7 IMPL-SUM — Server-side PROGRAMME SUMMARY PNG renderer.

Pillow-based render of the end-of-plan recap in the same visual idiom as
``ride_report_png.py`` (single shareable image, no JS, no new deps).

Layout (~1200×1600 px):
  1. Header — programme name + dates + weeks
  2. KPI tile row — FTP Δ, eFTP Δ, CTL gain, VO2max Δ
  3. 2×3 mini-chart grid:
        intensity-distribution stacked bar | polarization-index ring
        monotony line                       | compliance bar
        mean-max curve overlay              | Hooper trend line
  4. Totals strip — km / hours / kJ / elev_m
  5. Citations footer

References (cited inline + in commit):
- Stöggl & Sperlich 2014 (Front Physiol 5:33) — POL → +11.7% VO2peak (the
  improvement bar).
- Foster 1998 (Med Sci Sports Exerc 30:1164) — monotony target <2.0.
- Treff et al. 2019 (Front Physiol) — polarization index >2.0 = polarized.
- Hooper & Mackinnon 1995 — wellness composite.
- Coggan/Allen TR&P 3rd ed. — eFTP / CTL / TSS.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# Canvas
W, H = 1200, 1600
BG = (13, 17, 23)
CARD = (22, 28, 36)
TEXT = (230, 237, 243)
TEXT2 = (139, 148, 158)
TEXT3 = (101, 109, 118)
ACCENT = (88, 166, 255)
ACCENT2 = (140, 120, 255)
GREEN = (34, 197, 94)
RED = (239, 68, 68)
ORANGE = (249, 115, 22)
YELLOW = (234, 179, 8)
BORDER = (48, 54, 61)


_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    ordered = _FONT_CANDIDATES if not bold else [p for p in _FONT_CANDIDATES if "Bold" in p or "bd" in p] + _FONT_CANDIDATES
    for p in ordered:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _fmt_delta_w(start, end, pct):
    if start is None or end is None:
        return "—"
    delta = (end - start)
    sign = "+" if delta >= 0 else ""
    if pct is None:
        return f"{sign}{int(round(delta))}W"
    return f"{sign}{int(round(delta))}W ({sign}{pct:.1f}%)"


def _draw_kpi_tile(draw, x, y, w, h, label, value, sub=None, color=ACCENT):
    _rounded_rect(draw, (x, y, x + w, y + h), 12, fill=CARD, outline=BORDER)
    draw.text((x + 16, y + 12), label, font=_font(11, bold=True), fill=TEXT2)
    draw.text((x + 16, y + 36), value, font=_font(28, bold=True), fill=color)
    if sub:
        draw.text((x + 16, y + h - 24), sub, font=_font(10), fill=TEXT3)


def _draw_intensity_dist(draw, x, y, w, h, dist):
    """Stacked horizontal bar: Z1+Z2 / Z3 / Z4+ minutes."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Intensity distribution (min)", font=_font(11, bold=True), fill=TEXT2)
    if not dist:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    z1z2 = max(0, int(dist.get("z1z2_min", 0)))
    z3 = max(0, int(dist.get("z3_min", 0)))
    z4 = max(0, int(dist.get("z4plus_min", 0)))
    total = z1z2 + z3 + z4
    if total <= 0:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    bx, by, bw, bh = x + 18, y + 50, w - 36, 30
    cur_x = bx
    seg = [(z1z2, GREEN, "Z1+Z2"), (z3, YELLOW, "Z3"), (z4, RED, "Z4+")]
    for val, col, _lab in seg:
        sw = int(round(bw * val / total))
        if sw > 0:
            draw.rectangle((cur_x, by, cur_x + sw, by + bh), fill=col)
            cur_x += sw
    legend_y = by + bh + 16
    pct_z1z2 = round(100 * z1z2 / total)
    pct_z3 = round(100 * z3 / total)
    pct_z4 = round(100 * z4 / total)
    draw.text((x + 18, legend_y), f"Z1+Z2 {pct_z1z2}%", font=_font(11, bold=True), fill=GREEN)
    draw.text((x + w / 2 - 30, legend_y), f"Z3 {pct_z3}%", font=_font(11, bold=True), fill=YELLOW)
    draw.text((x + w - 18, legend_y), f"Z4+ {pct_z4}%", font=_font(11, bold=True), fill=RED, anchor="ra")
    draw.text((x + 18, y + h - 22), f"Total {total}min · target 80/0/20 (Stöggl 2014)",
              font=_font(9), fill=TEXT3)


def _draw_pol_ring(draw, x, y, w, h, pol):
    """Polarization-index ring with classification label."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Polarization index (Treff 2019)", font=_font(11, bold=True), fill=TEXT2)
    if not pol or pol.get("mean") is None:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    mean = pol.get("mean") or 0.0
    klass = pol.get("class") or "—"
    cx, cy = x + w / 2, y + h / 2 + 10
    r = min(w, h) / 3
    # Ring: full circle outline + arc proportional to PI / 4.0
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=BORDER, width=4)
    arc_end = max(0.0, min(1.0, mean / 4.0))  # PI of 4 = full ring
    end_angle = -90 + 360 * arc_end
    if arc_end > 0.001:
        col = GREEN if mean > 2.0 else YELLOW if mean > 1.0 else ORANGE
        draw.arc((cx - r, cy - r, cx + r, cy + r), -90, end_angle, fill=col, width=8)
    draw.text((cx, cy - 8), f"{mean:.2f}", font=_font(26, bold=True), fill=TEXT, anchor="mm")
    draw.text((cx, cy + 18), klass, font=_font(11), fill=TEXT2, anchor="mm")
    draw.text((x + w / 2, y + h - 18), "PI > 2.0 = polarized", font=_font(9), fill=TEXT3, anchor="mm")


def _draw_monotony(draw, x, y, w, h, monotony, strain):
    """Single value with target line."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Monotony / strain (Foster 1998)", font=_font(11, bold=True), fill=TEXT2)
    if monotony is None:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    color = GREEN if monotony < 2.0 else RED
    draw.text((x + w / 2, y + h / 2 - 12), f"{monotony:.2f}", font=_font(34, bold=True), fill=color, anchor="mm")
    draw.text((x + w / 2, y + h / 2 + 18), "monotony (target <2.0)", font=_font(10), fill=TEXT3, anchor="mm")
    if strain is not None:
        draw.text((x + w / 2, y + h - 22), f"max strain {int(round(strain))}",
                  font=_font(10), fill=TEXT3, anchor="mm")


def _draw_compliance(draw, x, y, w, h, compliance):
    """Per-phase planned-vs-actual TSS bar chart."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Compliance per phase (planned vs actual TSS)",
              font=_font(11, bold=True), fill=TEXT2)
    if not compliance:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    bx, by, bw, bh = x + 18, y + 32, w - 36, h - 60
    n = len(compliance)
    if n == 0:
        return
    bar_w = bw / max(1, n) - 10
    max_tss = max((max(c.get("planned_tss", 0), c.get("actual_tss", 0)) for c in compliance), default=1)
    if max_tss <= 0:
        max_tss = 1
    for i, entry in enumerate(compliance):
        ex = bx + i * (bar_w + 10)
        planned = entry.get("planned_tss", 0) or 0
        actual = entry.get("actual_tss", 0) or 0
        pct = entry.get("pct", 0) or 0
        # planned bar (background)
        ph = (planned / max_tss) * (bh - 40)
        draw.rectangle((ex, by + bh - 30 - ph, ex + bar_w, by + bh - 30), fill=BORDER)
        # actual bar (overlay)
        ah = (actual / max_tss) * (bh - 40)
        col = GREEN if 90 <= pct <= 110 else YELLOW if 70 <= pct <= 130 else RED
        draw.rectangle((ex + bar_w / 4, by + bh - 30 - ah, ex + 3 * bar_w / 4, by + bh - 30), fill=col)
        # phase label
        phase = (entry.get("phase", "") or "")[:8]
        draw.text((ex + bar_w / 2, by + bh - 22), phase, font=_font(9), fill=TEXT2, anchor="mm")
        draw.text((ex + bar_w / 2, by + bh - 8), f"{pct}%", font=_font(10, bold=True), fill=col, anchor="mm")


def _draw_mean_max(draw, x, y, w, h, mm):
    """5s/1m/5m/20m/60m mean-max curve, first 4w vs last 4w overlay."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Mean-max power curve (start vs end 4w)",
              font=_font(11, bold=True), fill=TEXT2)
    if not mm or not mm.get("end"):
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    cx, cy, cw, ch = x + 40, y + 32, w - 60, h - 60
    durations = [(5, "5s"), (60, "1m"), (300, "5m"), (1200, "20m"), (3600, "60m")]
    start = {d.get("dur"): d.get("watts") for d in (mm.get("start") or [])}
    end = {d.get("dur"): d.get("watts") for d in (mm.get("end") or [])}
    vals = [v for v in list(start.values()) + list(end.values()) if v]
    vmax = max(vals) if vals else 1
    if vmax <= 0:
        vmax = 1
    n = len(durations)
    xs = [cx + i * (cw / max(1, n - 1)) for i in range(n)]
    # End line
    end_pts = []
    start_pts = []
    for i, (dur, _lab) in enumerate(durations):
        sv = start.get(dur)
        ev = end.get(dur)
        if sv:
            start_pts.append((xs[i], cy + ch - (sv / vmax) * ch))
        if ev:
            end_pts.append((xs[i], cy + ch - (ev / vmax) * ch))
    if len(start_pts) >= 2:
        draw.line(start_pts, fill=TEXT3, width=2)
        for px, py in start_pts:
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=TEXT3)
    if len(end_pts) >= 2:
        draw.line(end_pts, fill=ACCENT, width=2)
        for px, py in end_pts:
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=ACCENT)
    for i, (_dur, lab) in enumerate(durations):
        draw.text((xs[i], cy + ch + 6), lab, font=_font(10), fill=TEXT3, anchor="mt")
    draw.text((x + w - 14, y + 30), "● end  ○ start", font=_font(10), fill=TEXT3, anchor="ra")


def _draw_hooper(draw, x, y, w, h, trend):
    """Weekly Hooper trend line."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), "Hooper composite trend (1995)",
              font=_font(11, bold=True), fill=TEXT2)
    if not trend:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    cx, cy, cw, ch = x + 40, y + 32, w - 60, h - 60
    means = [t.get("mean") for t in trend if t.get("mean") is not None]
    if not means:
        draw.text((x + w / 2, y + h / 2), "no data", font=_font(12), fill=TEXT3, anchor="mm")
        return
    vmax = max(28.0, max(means))
    vmin = min(4.0, min(means))
    rng = max(1.0, vmax - vmin)
    n = len(trend)
    pts = []
    for i, t in enumerate(trend):
        m = t.get("mean")
        if m is None:
            continue
        px = cx + i * (cw / max(1, n - 1))
        py = cy + ch - ((m - vmin) / rng) * ch
        pts.append((px, py))
    if len(pts) >= 2:
        draw.line(pts, fill=ACCENT2, width=2)
    for px, py in pts:
        draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=ACCENT2)
    # Threshold line at 18 (G6 cap)
    thresh_y = cy + ch - ((18 - vmin) / rng) * ch
    for dx in range(0, int(cw), 8):
        draw.line([(cx + dx, thresh_y), (cx + dx + 4, thresh_y)], fill=RED, width=1)
    draw.text((cx + cw - 4, thresh_y - 12), "Hooper=18 (cap)",
              font=_font(9), fill=RED, anchor="ra")


def render_programme_summary_png(summary: dict) -> bytes:
    """Render an end-of-plan programme summary as PNG bytes.

    Args:
        summary: dict matching the §4 contract (see MASTER_DECISIONS_v467.md).

    Returns:
        PNG bytes (1200×1600 px).
    """
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── Header ─────────────────────────────────────────────────────────────
    draw.text((40, 30), "DOMESTIQUE", font=_font(14, bold=True), fill=ACCENT)
    draw.text((40, 56), "Programme Summary", font=_font(36, bold=True), fill=TEXT)
    start = summary.get("start_date") or "—"
    end = summary.get("end_date") or "—"
    weeks = summary.get("weeks") or 0
    draw.text((40, 110), f"{start}  →  {end}   ·   {weeks} weeks",
              font=_font(16), fill=TEXT2)

    # ── KPI tile row ───────────────────────────────────────────────────────
    tile_y = 160
    tile_h = 110
    tile_w = (W - 80 - 30) // 4
    gap = 10
    ftp = summary.get("ftp_delta") or {}
    eftp = summary.get("eftp_delta") or {}
    ctl = summary.get("ctl_gain") or {}
    vo2 = summary.get("vo2max_delta") or {}

    def _kpi_color(pct, default=ACCENT):
        if pct is None:
            return TEXT2
        if pct > 5:
            return GREEN
        if pct > 0:
            return ACCENT
        return RED

    # The delta dicts always carry the keys, with None for "not measured" — so
    # dict.get(k, "—") never fires and an unridden plan printed "NoneW → NoneW".
    def _dash(v) -> str:
        return "—" if v is None else str(v)

    tiles = [
        ("FTP",
         _fmt_delta_w(ftp.get("start"), ftp.get("end"), ftp.get("pct")),
         f"{_dash(ftp.get('start'))}W → {_dash(ftp.get('end'))}W",
         _kpi_color(ftp.get("pct"))),
        ("eFTP",
         _fmt_delta_w(eftp.get("start"), eftp.get("end"), eftp.get("pct")),
         f"{_dash(eftp.get('start'))}W → {_dash(eftp.get('end'))}W",
         _kpi_color(eftp.get("pct"))),
        ("CTL FITNESS",
         (f"{ctl.get('delta'):+.1f}" if ctl.get("delta") is not None else "—"),
         f"{_dash(ctl.get('start'))} → {_dash(ctl.get('end'))}",
         _kpi_color(ctl.get("delta"))),
        ("VO2max",
         (f"{vo2.get('pct'):+.1f}%" if vo2.get("pct") is not None else "—"),
         f"{_dash(vo2.get('start'))} → {_dash(vo2.get('end'))}  · Stöggl bar +11.7%",
         _kpi_color(vo2.get("pct"))),
    ]
    for i, (lab, val, sub, col) in enumerate(tiles):
        tx = 40 + i * (tile_w + gap)
        _draw_kpi_tile(draw, tx, tile_y, tile_w, tile_h, lab, val, sub, col)

    # ── 2×3 mini-chart grid ────────────────────────────────────────────────
    grid_y = tile_y + tile_h + 20
    cell_w = (W - 80 - 20) // 2
    cell_h = 250
    grid_gap = 20

    pol = summary.get("pol_index") or {}
    monotony = summary.get("monotony_max")
    strain = summary.get("strain_max")
    compliance = summary.get("compliance") or []
    mm = summary.get("mean_max_curve") or {}
    hooper = summary.get("hooper_trend") or []

    cells = [
        (_draw_intensity_dist, summary.get("intensity_dist")),
        (_draw_pol_ring, pol),
        (_draw_monotony, (monotony, strain)),
        (_draw_compliance, compliance),
        (_draw_mean_max, mm),
        (_draw_hooper, hooper),
    ]

    for i, (fn, data) in enumerate(cells):
        col = i % 2
        row = i // 2
        cx = 40 + col * (cell_w + grid_gap)
        cy = grid_y + row * (cell_h + grid_gap)
        if fn is _draw_monotony:
            fn(draw, cx, cy, cell_w, cell_h, data[0], data[1])
        else:
            fn(draw, cx, cy, cell_w, cell_h, data)

    # ── Totals strip ───────────────────────────────────────────────────────
    totals_y = grid_y + 3 * cell_h + 3 * grid_gap - 10
    totals = summary.get("totals") or {}
    _rounded_rect(draw, (40, totals_y, W - 40, totals_y + 80), 10, fill=CARD, outline=BORDER)
    parts = [
        ("KM",       f"{int(totals.get('km', 0))}"),
        ("HOURS",    f"{float(totals.get('hours', 0)):.1f}"),
        ("kJ",       f"{int(totals.get('kj', 0))}"),
        ("ELEV (m)", f"{int(totals.get('elev_m', 0))}"),
    ]
    seg_w = (W - 80) // 4
    for i, (lab, val) in enumerate(parts):
        sx = 40 + i * seg_w
        draw.text((sx + seg_w / 2, totals_y + 18), lab,
                  font=_font(11, bold=True), fill=TEXT2, anchor="mm")
        draw.text((sx + seg_w / 2, totals_y + 50), val,
                  font=_font(28, bold=True), fill=TEXT, anchor="mm")

    # ── Citations footer ───────────────────────────────────────────────────
    cite_y = totals_y + 100
    citations = summary.get("citations") or []
    cite_text = " · ".join(citations) if citations else ""
    if cite_text:
        # Wrap if too long: split mid-list at width threshold.
        max_chars = 130
        if len(cite_text) > max_chars:
            half = len(citations) // 2 + 1
            line1 = " · ".join(citations[:half])
            line2 = " · ".join(citations[half:])
            draw.text((W / 2, cite_y), line1, font=_font(10), fill=TEXT3, anchor="mm")
            draw.text((W / 2, cite_y + 16), line2, font=_font(10), fill=TEXT3, anchor="mm")
        else:
            draw.text((W / 2, cite_y), cite_text, font=_font(10), fill=TEXT3, anchor="mm")
    draw.text((W - 40, H - 22), "Generated by Domestique",
              font=_font(10), fill=TEXT3, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
