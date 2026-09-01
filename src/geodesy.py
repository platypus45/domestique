"""Canonical geodesy helpers. Do NOT re-implement haversine elsewhere."""
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_M = 6_371_000.0


def haversine(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance in metres between (lat, lon) decimal-degree points."""
    lat1, lon1 = radians(p1[0]), radians(p1[1])
    lat2, lon2 = radians(p2[0]), radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))
