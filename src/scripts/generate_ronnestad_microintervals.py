#!/usr/bin/env python3
"""v3.7.1 — generate the Rønnestad-style 30/15 microinterval family.

ONE session shape, varied along two axes: how long you have, and how hard the
ON leg is. Nothing else changes — the point of this family is that the rider
learns one session and then picks the version that fits the day.

THE SHAPE
    plain warm-up ramp
    S series of N x (30 s ON / 15 s OFF)
    3 min easy between series
    cool-down

The warm-up is a SINGLE ramp on purpose. A multi-zone warm-up with drills and
openers is a different session's worth of decisions before the session that
matters has started, and the rider asked for none of it.

WHY EVERY FILE HERE IS VO2max, INCLUDING THE 125-135% ONES
    The zone table puts Z6 (anaerobic) at 1.21-1.51 x FTP, so 125% looks
    anaerobic and the obvious move is to split the family there. That is
    wrong, and the classifier is right to disagree: ZONES_FTP describes
    SUSTAINED efforts. A 30-second rep is not sustained. It is too short to
    be limited by anaerobic capacity, and the 15 s recovery is too short for
    oxygen uptake to fall — which is the entire mechanism of the protocol.

    classify_v104 models this and holds "vo2_short" all the way to ~1.50,
    flipping to neuromuscular only around 1.70 (measured: 1.13/1.25/1.35/1.50
    -> vo2_short, 1.70/2.00 -> neuromuscular). So the family spans 1.08-1.35
    as VO2max work and stays well inside that ceiling.

    What still changes with intensity is the REP COUNT, not the label.

THE REP/INTENSITY TRADE
    Reps per series come DOWN as the ON target goes UP. The protocol only
    works if the last series holds the same power as the first; a file whose
    final reps are unreachable is a file that trains fading, not VO2max.

WHAT THE SOURCE PROTOCOL ACTUALLY PRESCRIBES
    No paper in this family prescribes a % of FTP. The two landmark training
    studies told riders to hold "their maximal sustainable work intensity,
    aiming to achieve the highest possible average power output", seeded at
    the power that elicits VO2peak and re-adjusted between series. What elite
    riders ACHIEVED was 94 +/- 3 % of Wmax over 3 series and 86-88 % over 5;
    the only explicit power anchor the group has published is 30 s at ~118 %
    of 40-min maximal power with the OFF at ~60 %, governed by RPE 16-18.

    So the %FTP numbers here are a convenience, not the protocol. Every file
    says so in its own description: the target is the power you can repeat in
    the last series, and the percentage is only a starting point.

    The OFF leg is 50 % OF THE ON POWER, which is what both studies specify
    ("power output during the recovery periods was 50 % of the power output
    used during work intervals") — not a fixed 50 % of FTP. It therefore
    MOVES with the ON: 1.13 on -> 0.57 off, 1.35 on -> 0.68 off.

COOL-DOWN
    Follows the v3.7.0 rule (scripts/fix_cooldowns_v37.py): start at
    min(0.60, prev_end), end at min(0.45, start). The series recovery is 0.50,
    so every cool-down here runs 0.50 -> 0.45 and never steps up.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ON_S, OFF_S = 30, 15
SERIES_REST_S = 180
OFF_OF_ON = 0.50                    # the OFF leg is 50 % of the ON, not 50 % of FTP
CD_START_MAX, CD_END = 0.60, 0.45
CD_MIN_S = 300                      # 5 min floor — below this it is not a cool-down
NM_CEILING = 1.50                   # past ~1.55 a 30/15 reads neuromuscular

# (series, reps, on_frac). The DURATION is computed from the session rather
# than imposed on it — forcing a total minute count either starves the
# cool-down or pads the ramp, and both are worse than a file that is 58
# minutes instead of 60.
MATRIX = [
    # ── VO2max: Z5, built to accumulate time near VO2max ──────────────────
    (2, 10, 1.13), (2, 13, 1.10), (2, 13, 1.13), (2, 12, 1.16),
    (3, 9, 1.13), (3, 10, 1.16), (3, 11, 1.20),
    (3, 13, 1.08), (3, 13, 1.10), (3, 13, 1.13), (3, 12, 1.16),
    (4, 13, 1.10), (4, 12, 1.13), (4, 11, 1.16),
    (5, 12, 1.10),
    # ── the hard end: still VO2max by content, just fewer reps ───────────
    (2, 8, 1.25), (2, 10, 1.30),
    (3, 10, 1.25), (3, 9, 1.30), (3, 8, 1.35),
    (4, 9, 1.25),
]
assert all(on <= NM_CEILING for _s, _r, on in MATRIX), "past the VO2max ceiling"


def build(series: int, reps: int, on: float) -> "tuple[str, str]":
    """(filename, xml). Duration falls out of the session, never the reverse."""
    work_s = series * reps * (ON_S + OFF_S)
    rests_s = (series - 1) * SERIES_REST_S
    hard = on >= 1.21
    # A bigger session earns a longer ramp in; nothing fancy, just more of it.
    warm_s = 900 if work_s >= 2000 else (720 if work_s >= 1200 else 600)
    cool_s = 600 if work_s >= 2000 else 480
    total_min = round((warm_s + work_s + rests_s + cool_s) / 60)
    off = round(on * OFF_OF_ON, 2)
    cd_start = min(CD_START_MAX, off)
    cd_end = min(CD_END, cd_start)
    warm_hi = 0.78 if not hard else 0.80

    name = f"vo2_short_3015_{series}x{reps}_{round(on * 100)}pct_{total_min}min"
    title = (f"Rønnestad 30/15 VO2max — {series}x{reps} @ "
             f"{round(on * 100)}% ({total_min} min)")

    if hard:
        why = (f"The hard end of the family. {round(on * 100)}% FTP would be "
               f"anaerobic if you held it, but a 30-second rep is too short to "
               f"be limited by anaerobic capacity and the 15 s recovery is too "
               f"short for oxygen uptake to fall — so this is still VO2max work, "
               f"which is why it carries {reps} reps per series rather than "
               f"thirteen. Fresh legs only.")
    else:
        why = ("The 15 s recoveries are deliberately too short for oxygen uptake "
               "to fall back, so it ratchets up over the first few reps and stays "
               "near maximum for the rest of the series — which is the point. Time "
               "spent near VO2max is what drives the adaptation, and short "
               "on/off reps buy more of it than long intervals at the same "
               "average power.")

    desc = (
        f"{series} series of {reps} x (30s @ {round(on * 100)}% FTP / 15s easy), "
        f"3 min easy between series. {why} "
        f"The percentage is a STARTING POINT, not the protocol: the source "
        f"studies prescribed no percentage at all — ride the highest power you "
        f"can repeat in the LAST series and adjust between series. RPE 16-18. "
        f"A session you fade through trains fading. "
        f"Warmup: {warm_s // 60}min from 45% to {round(warm_hi * 100)}% FTP | "
        f"Microintervals: {series} x {reps} x (30s @ {round(on * 100)}% / 15s @ "
        f"{round(off * 100)}%) | "
        f"Cooldown: {cool_s // 60}min from {round(cd_start * 100)}% to "
        f"{round(cd_end * 100)}% FTP"
    )

    body = [f'        <Warmup Duration="{warm_s}" PowerLow="0.45" '
            f'PowerHigh="{warm_hi:.2f}" pace="0" Cadence="90" />']
    for i in range(series):
        msg = (f"Series {i + 1} of {series} — {reps} x 30/15. "
               + ("Same power as series 1." if i else
                  "Settle on a power you can repeat to the end."))
        body.append(
            f'        <IntervalsT Repeat="{reps}" OnDuration="{ON_S}" '
            f'OffDuration="{OFF_S}" OnPower="{on:.2f}" '
            f'OffPower="{off:.2f}" pace="0" Cadence="100" '
            f'CadenceResting="85">\n'
            f'            <textevent timeoffset="0" message="{msg}" />\n'
            f'        </IntervalsT>')
        if i < series - 1:
            body.append(
                f'        <SteadyState Duration="{SERIES_REST_S}" '
                f'Power="{off:.2f}" pace="0" Cadence="90">\n'
                f'            <textevent timeoffset="0" message="Easy 3 min — '
                f'spin, stay rolling." />\n'
                f'        </SteadyState>')
    body.append(f'        <Cooldown Duration="{cool_s}" '
                f'PowerLow="{cd_start:.2f}" PowerHigh="{cd_end:.2f}" '
                f'pace="0" Cadence="90" />')

    xml = ("<?xml version='1.0' encoding='utf-8'?>\n<workout_file>\n"
           "    <author>Domestique Library</author>\n"
           f"    <name>{title}</name>\n"
           f"    <description>{desc}</description>\n"
           "    <sportType>bike</sportType>\n    <workout>\n"
           + "\n".join(body) + "\n    </workout>\n</workout_file>\n")
    return f"{name}.zwo", xml


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="workouts", type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    made = []
    for series, reps, on in MATRIX:
        fn, xml = build(series, reps, on)
        made.append(fn)
        if args.apply:
            (args.dir / fn).write_text(xml, encoding="utf-8")

    v = sum(1 for f in made if f.startswith("vo2"))
    print(f"{'wrote' if args.apply else 'would write'} {len(made)} files "
          f"({v} VO2max, {len(made) - v} anaerobic)")
    for f in made:
        print("   ", f)
    if not args.apply:
        print("\ndry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
