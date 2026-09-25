"""Generate the synthetic demo exports: demo/timeline_ios.json and demo/timeline_android.json.

An invented Oahu resident's three weeks of driving (commutes, errands, weekend trips), routed on real
roads by the demo Valhalla (`drivelog demo --no-serve` starts it on port 8003), then thinned the way
Timeline exports are: a point every one to a few minutes, GPS jitter, the odd long gap, and some
drives that start a few minutes before the car moves. Deterministic (fixed seed).

    uv run python scripts/make_demo_data.py
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# scripts/inspect.py would shadow stdlib inspect (DEBUG.md, Phase 0): swap our dir for the project root.
sys.path[0] = str(Path(__file__).resolve().parents[1])

import httpx  # noqa: E402

from drivelog.match import decode_polyline6  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VALHALLA = "http://localhost:8003"
TZ = timezone(timedelta(hours=-10))  # Hawaii: no daylight saving
START = date(2016, 2, 29)            # three weeks, Monday to Sunday
DAYS = 21
rng = random.Random(20160229)

# Public places, used as rough destinations (Valhalla snaps each to the nearest road).
PLACES = {
    "home": (21.3935, -157.7400),        # Kailua
    "work": (21.3069, -157.8583),        # downtown Honolulu
    "grocery": (21.4010, -157.7445),     # Kailua town
    "mall": (21.2913, -157.8430),        # Ala Moana
    "gym": (21.4180, -157.8030),         # Kaneohe
    "waikiki": (21.2770, -157.8260),
    "north_shore": (21.5930, -158.1030),  # Haleiwa
    "hawaii_kai": (21.2870, -157.7050),
    "pearl_city": (21.3970, -157.9750),
    "kapolei": (21.3350, -158.0580),
    "waimanalo": (21.3420, -157.7120),
    "airport": (21.3310, -157.9210),
}


def route(a: str, b: str) -> tuple[list[tuple[float, float]], float, float]:
    """(shape [(lat, lng)], length m, time s) of the fastest route."""
    (la, na), (lb, nb) = PLACES[a], PLACES[b]
    r = httpx.post(f"{VALHALLA}/route", timeout=60, json={
        "locations": [{"lat": la, "lon": na}, {"lat": lb, "lon": nb}], "costing": "auto", "directions_type": "none"})
    r.raise_for_status()
    trip = r.json()["trip"]
    return decode_polyline6(trip["legs"][0]["shape"]), trip["summary"]["length"] * 1000, trip["summary"]["time"]


def _dist(p, q) -> float:
    dy = (q[0] - p[0]) * 111_320
    dx = (q[1] - p[1]) * 111_320 * math.cos(math.radians(p[0]))
    return math.hypot(dx, dy)


def drive(a: str, b: str, depart: datetime) -> dict:
    """One drive: points sampled along the route at the driver's pace, thinned like an export."""
    shape, length, secs = route(a, b)
    secs *= rng.uniform(0.75, 1.0)  # people drive a bit faster than routing engines expect
    parked = rng.choice([0, 0, 0, 120, 240])  # sometimes the app starts while the car is still parked
    cum = [0.0]
    for p, q in zip(shape, shape[1:]):
        cum.append(cum[-1] + _dist(p, q))
    total = cum[-1] or 1.0

    def at(t: float) -> tuple[float, float]:
        d = min(max(t / secs, 0), 1) * total
        i = max(1, next((k for k, c in enumerate(cum) if c >= d), len(cum) - 1))
        f = (d - cum[i - 1]) / ((cum[i] - cum[i - 1]) or 1)
        lat = shape[i - 1][0] + f * (shape[i][0] - shape[i - 1][0])
        lng = shape[i - 1][1] + f * (shape[i][1] - shape[i - 1][1])
        j = rng.gauss(0, 12) / 111_320  # ~12 m of GPS jitter
        return round(lat + j, 6), round(lng + j * rng.uniform(-1, 1), 6)

    pts, t = [], 0.0
    start = depart
    if parked:
        pts += [(start + timedelta(seconds=s), at(0)) for s in range(0, parked, 60)]
    while t < secs:
        pts.append((start + timedelta(seconds=parked + t), at(t)))
        t += rng.choice([60, 60, 120, 120, 180, 240]) + (rng.choice([0] * 12 + [600]))  # odd long gap
    end = start + timedelta(seconds=parked + secs)
    pts.append((end, at(secs)))
    return {"start": start, "end": end, "from": a, "to": b, "points": pts, "distance": length}


def week_plan(day: date) -> list[tuple[str, str, int, int]]:
    """(from, to, hour, minute) legs for a day."""
    wd = day.weekday()
    m = lambda base: base + rng.randint(-12, 12)  # noqa: E731
    legs: list[tuple[str, str, int, int]] = []
    if wd < 5:
        legs += [("home", "work", 7, m(40)), ("work", "home", 17, m(20))]
        if wd in (1, 3):
            legs += [("home", "gym", 18, m(40)), ("gym", "home", 20, m(0))]
        if wd == 2:
            legs += [("home", "grocery", 19, m(0)), ("grocery", "home", 19, m(50))]
        if wd == 4 and rng.random() < 0.7:
            legs += [("home", "waikiki", 19, m(0)), ("waikiki", "home", 22, m(30))]
    elif wd == 5:
        trip = rng.choice(["north_shore", "hawaii_kai", "kapolei"])
        legs += [("home", "grocery", 9, m(30)), ("grocery", "home", 10, m(20)),
                 ("home", trip, 11, m(30)), (trip, "home", 16, m(30))]
    else:
        legs += [("home", "waimanalo", 9, m(0)), ("waimanalo", "home", 12, m(30))]
        if rng.random() < 0.6:
            legs += [("home", "mall", 14, m(0)), ("mall", "home", 16, m(40))]
    if day == START + timedelta(days=11):
        legs += [("home", "airport", 5, m(40)), ("airport", "pearl_city", 7, m(0)), ("pearl_city", "home", 8, m(10))]
    return legs


def build() -> list[dict]:
    drives = []
    for i in range(DAYS):
        day = START + timedelta(days=i)
        for a, b, h, mnt in week_plan(day):
            hh, mm = divmod(h * 60 + mnt, 60)
            drives.append(drive(a, b, datetime(day.year, day.month, day.day, hh, mm, rng.randint(0, 59), tzinfo=TZ)))
    drives.sort(key=lambda d: d["start"])
    return drives


# ---------------------------------------------------------------- writers


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds")


def ios(drives: list[dict]) -> list[dict]:
    """Top-level array. Strings everywhere; path points are minute offsets from the path segment start."""
    out = []
    for prev, d in zip([None] + drives, drives):
        if prev:
            lat, lng = PLACES[d["from"]]
            out.append({"startTime": _iso(prev["end"]), "endTime": _iso(d["start"]),
                        "visit": {"hierarchyLevel": "0", "probability": "0.900000",
                                  "topCandidate": {"probability": "0.800000", "semanticType": "Unknown",
                                                   "placeID": f"demo-{d['from']}", "placeLocation": f"geo:{lat},{lng}"}}})
        (sl, sn), (el, en) = d["points"][0][1], d["points"][-1][1]
        out.append({"startTime": _iso(d["start"]), "endTime": _iso(d["end"]),
                    "activity": {"start": f"geo:{sl},{sn}", "end": f"geo:{el},{en}",
                                 "distanceMeters": f"{d['distance']:.6f}",
                                 "topCandidate": {"type": "in passenger vehicle", "probability": "0.000000"}}})
        seg_start = d["start"].replace(second=0, microsecond=0)
        out.append({"startTime": _iso(seg_start), "endTime": _iso(d["end"]),
                    "timelinePath": [{"point": f"geo:{lat},{lng}",
                                      "durationMinutesOffsetFromStartTime": str(int((t - seg_start).total_seconds() // 60))}
                                     for t, (lat, lng) in d["points"][1:-1]]})
    return out


def android(drives: list[dict]) -> dict:
    """Object with semanticSegments; path points carry absolute times; numbers are numbers."""
    segs = []
    fmt = lambda p: f"{p[0]}°, {p[1]}°"  # noqa: E731
    for d in drives:
        segs.append({"startTime": _iso(d["start"]), "endTime": _iso(d["end"]),
                     "startTimeTimezoneUtcOffsetMinutes": -600, "endTimeTimezoneUtcOffsetMinutes": -600,
                     "activity": {"start": {"latLng": fmt(d["points"][0][1])}, "end": {"latLng": fmt(d["points"][-1][1])},
                                  "distanceMeters": round(d["distance"], 1), "probability": 0.95,
                                  "topCandidate": {"type": "IN_PASSENGER_VEHICLE", "probability": 0.9}}})
        segs.append({"startTime": _iso(d["start"]), "endTime": _iso(d["end"]),
                     "timelinePath": [{"point": fmt(p), "time": _iso(t)} for t, p in d["points"][1:-1]]})
    return {"semanticSegments": segs, "rawSignals": [], "userLocationProfile": {}}


if __name__ == "__main__":
    ds = build()
    (ROOT / "demo/timeline_ios.json").write_text(json.dumps(ios(ds), indent=1, ensure_ascii=False))
    (ROOT / "demo/timeline_android.json").write_text(json.dumps(android(ds), indent=1, ensure_ascii=False))
    print(f"{len(ds)} drives, {sum(len(d['points']) for d in ds)} points, "
          f"{sum(d['distance'] for d in ds) / 1609.344:,.0f} mi, {START} → {START + timedelta(days=DAYS - 1)}")
