"""Coordinate parsing and distance helpers."""

from __future__ import annotations

import math
import re

# "geo:21.3069,-157.8583" (iOS) or "21.3069°, -157.8583°" (Android)
_COORD_RE = re.compile(r"^\s*(?:geo:)?\s*(-?\d+(?:\.\d+)?)°?\s*,\s*(-?\d+(?:\.\d+)?)°?\s*$")


def parse_coord(s) -> tuple[float, float] | None:
    if not isinstance(s, str):
        return None
    m = _COORD_RE.match(s)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def path_length_m(points) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
