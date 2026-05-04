"""Server-side RIDE REPORT PNG renderer.

Replaces the client-side html2canvas snapshot path, which could block
pywebview's main thread for several seconds on rides with dense elevation
+ power SVGs. Renders a clean 1600x900 summary card via Pillow (already
a runtime dep), streamable as image/png.

The PNG is intentionally a "summary card", not a pixel-accurate clone of
the in-browser RIDE REPORT: fixed layout, key stats, one power chart. This
is what users want to archive / post / print.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# Canvas
W, H = 1600, 900
BG = (13, 17, 23)          # --surface
CARD = (22, 28, 36)        # --surface2
TEXT = (230, 237, 243)     # --text
TEXT2 = (139, 148, 158)    # --text2
TEXT3 = (101, 109, 118)    # --text3
ACCENT = (88, 166, 255)    # --accent (blue)
ACCENT2 = (140, 120, 255)  # --accent2 (violet)
GREEN = (34, 197, 94)
RED = (239, 68, 68)
ORANGE = (249, 115, 22)
YELLOW = (234, 179, 8)
BORDER = (48, 54, 61)

# Font resolution — macOS ships Helvetica/Arial; Windows ships Arial;
# Linux usually ships DejaVu. Fall back to Pillow's bitmap default if
# none found (still legible, just not pretty).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Try bold candidates first when requested.
    ordered = _FONT_CANDIDATES if not bold else [p for p in _FONT_CANDIDATES if "Bold" in p or "bd" in p] + _FONT_CANDIDATES
    for p in ordered:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _hms(seconds: int | float | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def _fmt(v: Any, unit: str = "", decimals: int = 0) -> str:
    if v is None:
        return "—"
    try:
        if decimals:
            return f"{float(v):.{decimals}f}{unit}"
        return f"{int(round(float(v)))}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _rounded_rect(draw: ImageDraw.ImageDraw, xy, radius: int, fill=None, outline=None, width: int = 1) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_power_chart(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                      samples: list[float], ftp: float | None, title: str) -> None:
    """Line chart of power over time with FTP reference line."""
    _rounded_rect(draw, (x, y, x + w, y + h), 10, fill=CARD, outline=BORDER)
    draw.text((x + 14, y + 8), title, font=_font(12, bold=True), fill=TEXT2)

    if not samples:
        draw.text((x + w / 2, y + h / 2), "no power data", font=_font(14), fill=TEXT3, anchor="mm")
        return

    # Area inside padding
    pad_l, pad_r, pad_t, pad_b = 40, 14, 34, 18
    cx, cy, cw, ch = x + pad_l, y + pad_t, w - pad_l - pad_r, h - pad_t - pad_b

    # Downsample to ~400 points max
    n = len(samples)
    step = max(1, n // 400)
    xs, ys = [], []
    peak = max(samples) if samples else 1
    ymax = max(peak * 1.05, (ftp or 250) * 1.4)
    for i in range(0, n, step):
        val = samples[i]
        if val is None:
            continue
        xs.append(cx + (i / max(1, n - 1)) * cw)
        ys.append(cy + ch - (val / ymax) * ch)

    # FTP reference
    if ftp:
        ref_y = cy + ch - (ftp / ymax) * ch
        for dx in range(0, cw, 8):
            draw.line([(cx + dx, ref_y), (cx + dx + 4, ref_y)], fill=YELLOW, width=1)
        draw.text((cx + cw - 2, ref_y - 7), f"FTP {int(ftp)}W", font=_font(10), fill=YELLOW, anchor="ra")

    # Power line
    if len(xs) >= 2:
        pts = list(zip(xs, ys))
        draw.line(pts, fill=ACCENT, width=2)
        # Subtle fill under the line
        poly = pts + [(xs[-1], cy + ch), (xs[0], cy + ch)]
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.polygon([(px - x, py - y) for px, py in poly], fill=(88, 166, 255, 40))
        # Can't directly alpha-composite into draw; skip subtle fill for simplicity.

    # Y-axis ticks
    for frac in (0, 0.5, 1.0):
        ty = cy + ch - frac * ch
        draw.line([(cx, ty), (cx + cw, ty)], fill=BORDER, width=1)
        draw.text((cx - 6, ty), f"{int(ymax * frac)}", font=_font(10), fill=TEXT3, anchor="rm")


def render_ride_report_png(summary: dict, samples: dict, profile: dict | None = None) -> bytes:
    """Render a ride report as PNG bytes.

    Args:
        summary: ride summary dict (name, duration_sec, avg_power, weighted_power,
                 tss, calories, distance_km, avg_hr, max_hr, normalized_power,
                 course_name, workout_name, date/started_at, etc.)
        samples: {"power": [...], "hr": [...], "time_sec": [...], ...}
        profile: {"ftp": int, "lthr": int, ...} (optional; used for FTP ref line)
    """
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Header
    name = summary.get("name") or summary.get("course_name") or summary.get("workout_name") or "Ride"
    # Build a date string from whatever is available
    date_str = summary.get("date") or summary.get("started_at") or ""
    if "T" in str(date_str):
        date_str = str(date_str).split("T", 1)[0]
    duration_s = summary.get("duration_sec") or summary.get("elapsed_sec") or summary.get("moving_time_sec") or 0

    draw.text((50, 40), "DOMESTIQUE", font=_font(18, bold=True), fill=ACCENT)
    draw.text((50, 70), str(name)[:80], font=_font(42, bold=True), fill=TEXT)
    subtitle_bits = []
    if date_str:
        subtitle_bits.append(str(date_str))
    if duration_s:
        subtitle_bits.append(_hms(duration_s))
    if subtitle_bits:
        draw.text((50, 130), " · ".join(subtitle_bits), font=_font(18), fill=TEXT2)

    # Stats grid — 6 cards in a 3×2 arrangement below the header
    stats = [
        ("TSS",          _fmt(summary.get("tss"), "")),
        ("AVG POWER",    _fmt(summary.get("avg_power"), "W")),
        ("NP",           _fmt(summary.get("normalized_power") or summary.get("weighted_power"), "W")),
        ("AVG HR",       _fmt(summary.get("avg_hr"), " bpm")),
        ("DISTANCE",     _fmt(summary.get("distance_km"), " km", 1)),
        ("CALORIES",     _fmt(summary.get("calories") or summary.get("kcal") or summary.get("total_kj"), " kcal")),
    ]
    card_w, card_h = 230, 110
    grid_x = 50
    grid_y = 180
    gap = 20
    for i, (label, value) in enumerate(stats):
        col, row = i % 3, i // 3
        x = grid_x + col * (card_w + gap)
        y = grid_y + row * (card_h + gap)
        _rounded_rect(draw, (x, y, x + card_w, y + card_h), 10, fill=CARD, outline=BORDER)
        draw.text((x + 16, y + 14), label, font=_font(11, bold=True), fill=TEXT2)
        draw.text((x + 16, y + 38), value, font=_font(34, bold=True), fill=TEXT)

    # Power chart (right side, full height of grid area)
    chart_x = grid_x + 3 * (card_w + gap) + 10
    chart_y = grid_y
    chart_w = W - chart_x - 50
    chart_h = 2 * card_h + gap
    power_samples = samples.get("power") if samples else []
    ftp = (profile or {}).get("ftp")
    _draw_power_chart(draw, chart_x, chart_y, chart_w, chart_h, power_samples or [], ftp, "Power over time")

    # HR chart (below, full width)
    hr_samples = samples.get("hr") if samples else []
    if hr_samples:
        hx, hy = 50, grid_y + 2 * (card_h + gap) + 20
        hw, hh = W - 100, H - hy - 40
        _rounded_rect(draw, (hx, hy, hx + hw, hy + hh), 10, fill=CARD, outline=BORDER)
        draw.text((hx + 14, hy + 8), "Heart rate over time", font=_font(12, bold=True), fill=TEXT2)
        pad_l, pad_r, pad_t, pad_b = 40, 14, 34, 18
        cx, cy, cw, ch = hx + pad_l, hy + pad_t, hw - pad_l - pad_r, hh - pad_t - pad_b
        valid = [v for v in hr_samples if v]
        if valid:
            vmax, vmin = max(valid), min(valid)
            rng = max(1, vmax - vmin)
            xs, ys = [], []
            step = max(1, len(hr_samples) // 400)
            for i in range(0, len(hr_samples), step):
                v = hr_samples[i]
                if v is None or v <= 0:
                    continue
                xs.append(cx + (i / max(1, len(hr_samples) - 1)) * cw)
                ys.append(cy + ch - ((v - vmin) / rng) * ch)
            if len(xs) >= 2:
                draw.line(list(zip(xs, ys)), fill=RED, width=2)
            draw.text((cx - 6, cy), str(int(vmax)), font=_font(10), fill=TEXT3, anchor="rm")
            draw.text((cx - 6, cy + ch), str(int(vmin)), font=_font(10), fill=TEXT3, anchor="rm")

    # Footer
    draw.text((W - 50, H - 22), "Generated by Domestique", font=_font(10), fill=TEXT3, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
