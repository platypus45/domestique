"""Synthesise a .zwo that hits a prescribed zone dose exactly.

WHY THIS EXISTS. The planner asks a slot for a dose -- "18 minutes above 106%
FTP in a 60-minute window" -- and then searches a fixed library for a file that
happens to contain it. Measured on this library: the median score>=5 file
carries 1.5 minutes above 106% FTP and 54 minutes of tempo, and the pool as a
whole sits at 53.8/39.1/7.1. A weighted draw from that pool returns the pool's
shape whatever the budget asks for. Pushing the scorer harder does not help --
swept against the safety rails, more authority just moves the breach to a
different band.

So: stop searching, start constructing. Given a dose and a window, emit a
workout that delivers it by arithmetic.

WHAT THIS IS NOT. It does not replace the library. Curated files carry names,
provenance and human judgement a generator cannot. This is for the slots the
library cannot serve -- ask it first, generate when the best candidate's
residual is too large.

PROVENANCE. Every protocol here is a published one, parameterised within the
bounds its own paper used, and each generated file records which. A generated
workout that cannot say where it came from has no business in a training plan.
"""
from __future__ import annotations

from dataclasses import dataclass

# Coggan %FTP edges, mirroring training_planner._acc_zone and zones.py. The
# three-zone bands: z1 <76%, z2 76-105%, z3 >=106%.
_Z3_FLOOR_PCT = 106.0
_Z2_FLOOR_PCT = 76.0


@dataclass(frozen=True)
class Protocol:
    """A published interval protocol, with the bounds its source used.

    ``on_pct`` is %FTP for the work leg, ``off_pct`` for the recovery. Rep and
    recovery seconds are (min, max) ranges; the solver picks inside them.
    """
    key: str
    label: str
    source: str
    on_pct: float
    off_pct: float
    rep_s: tuple[int, int]
    rest_s: tuple[int, int]
    reps_per_set: tuple[int, int]
    sets: tuple[int, int]
    set_rest_s: int = 300

    def band(self) -> str:
        """Which three-zone band this protocol's work leg lands in."""
        if self.on_pct >= _Z3_FLOOR_PCT:
            return "z3"
        if self.on_pct >= _Z2_FLOOR_PCT:
            return "z2"
        return "z1"


# The protocols the repo already cites in docs/SCIENCE_REVIEW.md, with the
# parameters their papers used. Nothing invented: a generated session is one of
# these with its rep count and set count solved for the prescribed dose.
PROTOCOLS: dict[str, Protocol] = {
    "ronnestad_30_15": Protocol(
        key="ronnestad_30_15", label="Rønnestad 30/15 micro-intervals",
        source="Rønnestad & Hansen 2014; Rønnestad et al. 2020",
        on_pct=110.0, off_pct=50.0,
        rep_s=(30, 30), rest_s=(15, 15),
        reps_per_set=(10, 13), sets=(2, 4), set_rest_s=180),
    "seiler_4x8": Protocol(
        key="seiler_4x8", label="Seiler 4x8min",
        source="Seiler et al. 2013 (PMID 21812820)",
        on_pct=106.0, off_pct=50.0,
        rep_s=(360, 600), rest_s=(120, 180),
        reps_per_set=(3, 5), sets=(1, 1)),
    "helgerud_4x4": Protocol(
        key="helgerud_4x4", label="Helgerud 4x4min",
        source="Helgerud et al. 2007",
        on_pct=112.0, off_pct=55.0,
        rep_s=(240, 240), rest_s=(180, 180),
        reps_per_set=(3, 5), sets=(1, 1)),
    "vo2_5x3": Protocol(
        key="vo2_5x3", label="VO2max 5x3min",
        source="Buchheit & Laursen 2013 (short-interval HIIT)",
        on_pct=118.0, off_pct=50.0,
        rep_s=(150, 240), rest_s=(150, 180),
        reps_per_set=(4, 6), sets=(1, 1)),
    "over_under": Protocol(
        key="over_under", label="Over-unders",
        source="Coggan & Allen, Training and Racing with a Power Meter 3rd ed.",
        on_pct=100.0, off_pct=88.0,
        rep_s=(120, 300), rest_s=(120, 240),
        reps_per_set=(3, 6), sets=(1, 2), set_rest_s=300),
    "threshold_2x20": Protocol(
        key="threshold_2x20", label="Threshold 2x20min",
        source="Coggan & Allen, Training and Racing with a Power Meter 3rd ed.",
        on_pct=98.0, off_pct=55.0,
        rep_s=(900, 1500), rest_s=(300, 600),
        reps_per_set=(2, 3), sets=(1, 1)),
    "sweetspot_3x15": Protocol(
        key="sweetspot_3x15", label="Sweet spot 3x15min",
        source="Coggan sweet-spot band, 88-94% FTP",
        on_pct=90.0, off_pct=55.0,
        rep_s=(600, 1200), rest_s=(300, 480),
        reps_per_set=(2, 4), sets=(1, 1)),
}

# Which protocols a session_type may draw on, hardest-first within each.
PROTOCOLS_FOR_TYPE: dict[str, tuple[str, ...]] = {
    "vo2max": ("ronnestad_30_15", "helgerud_4x4", "vo2_5x3"),
    "threshold": ("seiler_4x8", "threshold_2x20"),
    "overunder": ("over_under",),
    "sweetspot": ("sweetspot_3x15",),
    "tempo": ("sweetspot_3x15",),
}

_WARMUP_S = 900          # 15 min, 50 -> 70% FTP
_COOLDOWN_S = 300        # 5 min, 55 -> 40% FTP
_WARMUP_LOW, _WARMUP_HIGH = 0.50, 0.70
_COOLDOWN_HIGH, _COOLDOWN_LOW = 0.55, 0.40


@dataclass(frozen=True)
class Session:
    """A solved session: the protocol, its parameters, and what it delivers."""
    protocol: Protocol
    sets: int
    reps_per_set: int
    rep_s: int
    rest_s: int
    total_s: int
    band_s: dict[str, float]      # seconds in each three-zone band

    @property
    def work_s(self) -> int:
        return self.sets * self.reps_per_set * self.rep_s


def _ramp_band_seconds(lo: float, hi: float, dur_s: int) -> dict[str, float]:
    """Seconds each band gets from a linear ramp, at 1-second resolution.

    Same resolution the library scanner uses. A ramp is a straight line; there
    is no reason to approximate it.
    """
    out = {"z1": 0.0, "z2": 0.0, "z3": 0.0}
    n = max(1, int(dur_s))
    for i in range(n):
        pct = (lo + (hi - lo) * (i + 0.5) / n) * 100.0
        band = "z3" if pct >= _Z3_FLOOR_PCT else ("z2" if pct >= _Z2_FLOOR_PCT else "z1")
        out[band] += dur_s / n
    return out


def _band_of(pct: float) -> str:
    return "z3" if pct >= _Z3_FLOOR_PCT else ("z2" if pct >= _Z2_FLOOR_PCT else "z1")


def solve(protocol: Protocol, target_band_s: float, window_s: int) -> Session | None:
    """Pick the parameters that put ``target_band_s`` seconds in the protocol's
    own band, inside ``window_s``.

    Searches the protocol's published parameter ranges only -- it will return
    None rather than emit a session the source never described. The caller then
    tries the next protocol, or falls back to the library.
    """
    band = protocol.band()
    fixed_s = _WARMUP_S + _COOLDOWN_S
    best: Session | None = None
    best_err = float("inf")

    for sets in range(protocol.sets[0], protocol.sets[1] + 1):
        for reps in range(protocol.reps_per_set[0], protocol.reps_per_set[1] + 1):
            for rep_s in range(protocol.rep_s[0], protocol.rep_s[1] + 1,
                               max(1, (protocol.rep_s[1] - protocol.rep_s[0]) // 8 or 1)):
                for rest_s in range(protocol.rest_s[0], protocol.rest_s[1] + 1,
                                    max(1, (protocol.rest_s[1] - protocol.rest_s[0]) // 4 or 1)):
                    work_s = sets * reps * rep_s
                    # IntervalsT emits `reps` ON legs AND `reps` OFF legs -- the
                    # last rep gets a recovery too. Assuming (reps-1) put the
                    # accounting 4 minutes under what the scanner measured on
                    # the file this same function had just written. The round
                    # trip caught it; arithmetic alone would not have.
                    rest_total = sets * reps * rest_s + (sets - 1) * protocol.set_rest_s
                    total = fixed_s + work_s + rest_total
                    if total > window_s:
                        continue

                    # Score the BAND, not the work. An over-under recovers at
                    # 88% FTP, which is the same band as its 100% work leg, so
                    # counting only the work legs asked for 40 minutes of Z2 and
                    # delivered 52. What the plan budgets is time in the band.
                    bands = {"z1": 0.0, "z2": 0.0, "z3": 0.0}
                    for k, v in _ramp_band_seconds(_WARMUP_LOW, _WARMUP_HIGH,
                                                   _WARMUP_S).items():
                        bands[k] += v
                    for k, v in _ramp_band_seconds(_COOLDOWN_LOW, _COOLDOWN_HIGH,
                                                   _COOLDOWN_S).items():
                        bands[k] += v
                    bands[band] += work_s
                    bands[_band_of(protocol.off_pct)] += rest_total

                    err = abs(bands[band] - target_band_s)
                    if err < best_err:
                        best_err = err
                        best = Session(protocol, sets, reps, rep_s, rest_s, total, bands)
    return best


def to_zwo(s: Session, name: str, description: str) -> str:
    """Render a solved session as Zwift .zwo XML.

    Emits the same element vocabulary the library scanner parses -- Warmup,
    IntervalsT, Cooldown -- so a generated file is indistinguishable to every
    downstream consumer (the classifier, workout_facts, the FIT builder).
    """
    import xml.etree.ElementTree as ET

    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = "Domestique (generated)"
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "description").text = description
    ET.SubElement(root, "sportType").text = "bike"
    tags = ET.SubElement(root, "tags")
    for tag in ("generated", s.protocol.key):
        ET.SubElement(tags, "tag", name=tag)

    w = ET.SubElement(root, "workout")
    ET.SubElement(w, "Warmup", Duration=str(_WARMUP_S),
                  PowerLow=f"{_WARMUP_LOW}", PowerHigh=f"{_WARMUP_HIGH}")
    for i in range(s.sets):
        ET.SubElement(w, "IntervalsT",
                      Repeat=str(s.reps_per_set),
                      OnDuration=str(s.rep_s), OffDuration=str(s.rest_s),
                      OnPower=f"{s.protocol.on_pct / 100:.3f}",
                      OffPower=f"{s.protocol.off_pct / 100:.3f}")
        if i < s.sets - 1 and s.protocol.set_rest_s > 0:
            ET.SubElement(w, "SteadyState", Duration=str(s.protocol.set_rest_s),
                          Power=f"{s.protocol.off_pct / 100:.3f}")
    ET.SubElement(w, "Cooldown", Duration=str(_COOLDOWN_S),
                  PowerLow=f"{_COOLDOWN_LOW}", PowerHigh=f"{_COOLDOWN_HIGH}")

    ET.indent(root, space="    ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode")


def generate(session_type: str, window_min: float,
             target_band_min: float) -> tuple[str, str, Session] | None:
    """Build a session of ``session_type`` delivering ``target_band_min``
    minutes in its own band, inside ``window_min``.

    Returns (filename, zwo_xml, session) or None when no cited protocol can do
    it in that window -- which is a real answer, not a failure: it means the
    dose does not fit the time, and the caller should shorten the dose rather
    than invent a protocol.
    """
    window_s = int(window_min * 60)
    target_s = float(target_band_min) * 60.0
    # Try every cited protocol for this type and keep the CLOSEST, not the
    # first that returns anything: Rønnestad 30/15 tops out around 26 minutes
    # at VO2max within its published bounds, so a 30-minute dose belongs to a
    # longer-rep protocol even though 30/15 would happily return its maximum.
    best = None
    best_err = float("inf")
    for key in PROTOCOLS_FOR_TYPE.get(session_type, ()):
        proto = PROTOCOLS[key]
        s = solve(proto, target_s, window_s)
        if s is None:
            continue
        err = abs(s.band_s[proto.band()] - target_s)
        if err < best_err:
            best_err, best = err, (proto, s)
    if best is None:
        return None
    proto, s = best
    got = s.band_s[proto.band()] / 60.0
    # Deterministic name: same request -> same file, so the content classifier
    # and workout_facts caches stay valid across regenerations.
    fname = (f"gen_{proto.key}_{s.sets}x{s.reps_per_set}x{s.rep_s}s"
             f"-{s.rest_s}s_{int(proto.on_pct)}pct_{round(s.total_s / 60)}min.zwo")
    desc = (f"{proto.label} — {s.sets}x{s.reps_per_set}x{s.rep_s}s @"
            f"{int(proto.on_pct)}% FTP, {s.rest_s}s recovery. "
            f"{got:.0f} min in {proto.band()}. Source: {proto.source}. "
            f"Generated to a prescribed dose.")
    return fname, to_zwo(s, proto.label, desc), s
