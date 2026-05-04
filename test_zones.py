"""Boundary and edge-case tests for the canonical zones module."""
import pytest

import zones
from zones import (
    Zone,
    power_zones,
    hr_zones,
    power_zone_at,
    hr_zone_at,
    zone_distribution,
)


class TestPowerZones:
    def test_power_zones_returns_seven_zones(self):
        zs = power_zones(250)
        assert len(zs) == 7
        assert all(isinstance(z, Zone) for z in zs)

    def test_power_zones_z1_anchored_at_zero(self):
        zs = power_zones(250)
        assert zs[0].low == 0

    def test_power_zones_top_zone_open_ended(self):
        zs = power_zones(250)
        assert zs[-1].high == 99999
        assert zs[-1].name == "Z7 Neuromuscular"

    def test_power_zones_ascending(self):
        zs = power_zones(300)
        for i in range(1, len(zs)):
            assert zs[i].low > zs[i - 1].low
            assert zs[i].high > zs[i - 1].high

    def test_power_zones_ftp_zero_raises(self):
        with pytest.raises(ValueError):
            power_zones(0)

    def test_power_zones_ftp_negative_raises(self):
        with pytest.raises(ValueError):
            power_zones(-100)


class TestHRZones:
    def test_hr_zones_returns_five_zones(self):
        zs = hr_zones(165, 195)
        assert len(zs) == 5

    def test_hr_zones_top_clamped_to_max_hr(self):
        zs = hr_zones(165, 195)
        assert zs[-1].high == 195

    def test_hr_zones_top_open_ended_when_no_max_hr(self):
        zs = hr_zones(165)
        assert zs[-1].high == 99999

    def test_hr_zones_lthr_zero_raises(self):
        with pytest.raises(ValueError):
            hr_zones(0)

    def test_hr_zones_z1_anchored_at_zero(self):
        zs = hr_zones(170, 200)
        assert zs[0].low == 0


class TestZoneAt:
    def test_power_zone_at_zero_returns_zero(self):
        assert power_zone_at(0, 250) == 0

    def test_power_zone_at_negative_returns_zero(self):
        assert power_zone_at(-50, 250) == 0

    def test_power_zone_at_z3_tempo(self):
        # 200W at FTP=250 -> 80% FTP -> Z3 Tempo
        assert power_zone_at(200, 250) == 3

    def test_power_zone_at_above_top_returns_top(self):
        # 5000W is well above any reasonable Z7 lower bound; should map to 7.
        assert power_zone_at(5000, 250) == 7

    def test_power_zone_at_z2_z3_boundary(self):
        zs = power_zones(250)
        # Sample exactly at Z3 lower bound -> Z3
        assert power_zone_at(zs[2].low, 250) == 3
        # Sample exactly at Z2 upper bound -> Z2
        assert power_zone_at(zs[1].high, 250) == 2

    def test_hr_zone_at_zero_returns_zero(self):
        assert hr_zone_at(0, 165, 195) == 0

    def test_hr_zone_at_above_top_returns_top(self):
        assert hr_zone_at(250, 165, 195) == 5


class TestHRZoneBandMarker:
    """URG-R3-2: the zone-band marker in training.html maps the live BPM
    onto a [0, 100]% position. These tests pin the Python half of that
    calculation (the zones + `hr_zone_at`) so the template never reads
    stale or unphysical zone boundaries.

    Zone convention documented in `zones.py` (Friel 5-zone, LTHR-anchored):
        Z1 Recovery   < 81% LTHR   (anchor 0)
        Z2 Aerobic    81-89%
        Z3 Tempo      90-93%
        Z4 Threshold  94-102%
        Z5 VO2max     103%+ (clamped at max_hr)

    These are Friel's canonical LTHR percentages — the band ordering + cut
    points in `zones._HR_FRACS`. Any regression here breaks the live
    marker position in training.html.
    """

    def test_hr_150_at_lthr_160_lands_in_z4(self):
        """150 / 160 = 93.75% LTHR — squarely inside Z4 (94-102%)?
        Actually 93.75% is < 94 — but z3 upper bound is round(0.93*160)=149.
        So 150 lands in Z4 via the rounding-gap reclaim rule."""
        assert hr_zone_at(150, lthr=160, max_hr=190) == 4

    def test_hr_140_at_lthr_160_lands_in_z2(self):
        """140 / 160 = 87.5% LTHR — inside Z2 (81-89%)."""
        assert hr_zone_at(140, lthr=160, max_hr=190) == 2

    def test_hr_at_lthr_boundary_is_z4(self):
        """HR == LTHR (100%) lands in Z4 Threshold — core semantic anchor."""
        assert hr_zone_at(160, lthr=160, max_hr=190) == 4

    def test_hr_zone_ranges_cover_full_span_no_gaps(self):
        """Every integer HR between 0 and max_hr must map to exactly one
        zone in [1..5]. Rounding gaps between adjacent zones are handled
        by `_zone_for_value` — verify end-to-end coverage."""
        for bpm in range(1, 200):
            z = hr_zone_at(bpm, lthr=160, max_hr=195)
            assert 1 <= z <= 5, f"hr={bpm} mapped to zone {z}"

    def test_zone_band_local_fraction_for_marker(self):
        """Simulate what the template does: zi + (hr - low)/(high - low)
        -> a [0, 5) global position. Check the math for hr=150, LTHR=160
        (Z4 zone, low/high around 150/163 depending on rounding)."""
        zs = hr_zones(lthr=160, max_hr=195)
        hr = 150
        zi = hr_zone_at(hr, lthr=160, max_hr=195) - 1
        z = zs[zi]
        local = (hr - z.low) / (z.high - z.low)
        assert 0 <= local <= 1, local
        global_frac = (zi + local) / 5
        assert 0 <= global_frac < 1.0, global_frac


class TestZoneDistribution:
    def test_distribution_sums_durations_per_zone(self):
        zs = power_zones(250)
        # 100W (Z1) for 60s, 200W (Z3) for 30s
        samples = [(100, 60), (200, 30)]
        dist = zone_distribution(samples, zs)
        assert dist[0] == 60
        assert dist[2] == 30
        assert sum(dist) == 90

    def test_distribution_skips_nonpositive_values(self):
        zs = power_zones(250)
        samples = [(0, 60), (-50, 30), (200, 10)]
        dist = zone_distribution(samples, zs)
        assert sum(dist) == 10

    def test_distribution_skips_nonpositive_durations(self):
        zs = power_zones(250)
        samples = [(200, 0), (200, -5), (200, 10)]
        dist = zone_distribution(samples, zs)
        assert sum(dist) == 10
