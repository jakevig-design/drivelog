"""Phase 1: extract drives from a Timeline export into DuckDB.

Each export format has an adapter that yields raw activities and raw point fixes.
`build_drives` turns those into the common `Drive` model, independent of format.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ijson
import pyarrow as pa

from .config import Config, load_config
from .db import connect
from .geo import haversine_m, parse_coord, path_length_m

# Points implying more than this speed to their neighbours are dropped as outliers. Timeline
# path times are minute-rounded, so intervals are floored at 60 s before computing speed.
MAX_SPEED_KMH = 250


@dataclass
class RawActivity:
    start: datetime  # tz-aware, as in source
    end: datetime
    type: str
    probability: float | None
    distance_m: float | None
    start_coord: tuple[float, float] | None
    end_coord: tuple[float, float] | None


@dataclass
class RawPoint:
    ts: datetime  # tz-aware
    lat: float
    lng: float
    source: str  # 'path' or 'raw'


@dataclass
class Drive:
    drive_id: str
    start_time: datetime  # UTC, naive
    end_time: datetime
    local_date: str
    local_start: datetime  # naive local wall time
    utc_offset_min: int
    points: list[tuple[datetime, float, float]] = field(default_factory=list)
    reported_distance_m: float | None = None
    activity_confidence: float | None = None
    moving_start: datetime | None = None  # UTC, naive: start/end without parked lead-in and tail
    moving_end: datetime | None = None


def activity_type(t: str) -> str:
    """iOS says 'in passenger vehicle', Android 'IN_PASSENGER_VEHICLE': compare them as one."""
    return t.strip().lower().replace("_", " ")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- adapters


def detect_format(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(4096).lstrip()
    if head.startswith(b"["):
        return "ios"
    if head.startswith(b"{"):
        with path.open("rb") as f:
            for prefix, event, value in ijson.parse(f):
                if prefix == "" and event == "map_key" and value == "semanticSegments":
                    return "android"
    return "unknown"


class IosAdapter:
    """Top-level array. Path points are minute offsets from the path segment start."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> tuple[list[RawActivity], list[RawPoint]]:
        acts, pts = [], []
        with self.path.open("rb") as f:
            for seg in ijson.items(f, "item"):
                start, end = _dt(seg["startTime"]), _dt(seg["endTime"])
                if "activity" in seg:
                    a = seg["activity"]
                    top = a.get("topCandidate") or {}
                    acts.append(RawActivity(
                        start, end, top.get("type", ""), _f(top.get("probability")),
                        _f(a.get("distanceMeters")), parse_coord(a.get("start")), parse_coord(a.get("end")),
                    ))
                elif "timelinePath" in seg:
                    for p in seg["timelinePath"]:
                        c = parse_coord(p.get("point"))
                        off = _f(p.get("durationMinutesOffsetFromStartTime"))
                        if c and off is not None:
                            pts.append(RawPoint(start + timedelta(minutes=off), c[0], c[1], "path"))
        return acts, pts


class AndroidAdapter:
    """Object with semanticSegments (+ rawSignals). Path points carry absolute `time`."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> tuple[list[RawActivity], list[RawPoint]]:
        acts, pts = [], []
        with self.path.open("rb") as f:
            for seg in ijson.items(f, "semanticSegments.item"):
                if "activity" in seg:
                    a = seg["activity"]
                    top = a.get("topCandidate") or {}
                    acts.append(RawActivity(
                        _dt(seg["startTime"]), _dt(seg["endTime"]), top.get("type", ""),
                        _f(top.get("probability") or a.get("probability")), _f(a.get("distanceMeters")),
                        parse_coord((a.get("start") or {}).get("latLng")),
                        parse_coord((a.get("end") or {}).get("latLng")),
                    ))
                for p in seg.get("timelinePath") or []:
                    c = parse_coord(p.get("point"))
                    if c and p.get("time"):
                        pts.append(RawPoint(_dt(p["time"]), c[0], c[1], "path"))
        with self.path.open("rb") as f:
            for sig in ijson.items(f, "rawSignals.item"):
                pos = sig.get("position")
                if not pos:
                    continue
                c = parse_coord(pos.get("LatLng") or pos.get("latLng"))
                ts = pos.get("timestamp")
                if c and ts:
                    pts.append(RawPoint(_dt(ts), c[0], c[1], "raw"))
        return acts, pts


ADAPTERS = {"ios": IosAdapter, "android": AndroidAdapter}


# ---------------------------------------------------------------- drive building


def _kmh(a, b) -> float:
    dt = max(abs((b[0] - a[0]).total_seconds()), 60)
    return haversine_m(a[1], a[2], b[1], b[2]) / dt * 3.6


def drop_outliers(seq: list[tuple]) -> list[tuple]:
    """Iteratively drop points that are implausibly far from their neighbours. An interior point must
    be too fast to *both* neighbours (so one bad point doesn't take out a good one); an endpoint only
    has one neighbour. About 5% of activities have start/end coordinates hundreds of km off."""
    pts = list(seq)
    while len(pts) > 2:
        bad = None
        for i, p in enumerate(pts):
            prev_fast = i > 0 and _kmh(pts[i - 1], p) > MAX_SPEED_KMH
            next_fast = i < len(pts) - 1 and _kmh(p, pts[i + 1]) > MAX_SPEED_KMH
            if (i == 0 and next_fast) or (i == len(pts) - 1 and prev_fast) or (prev_fast and next_fast):
                bad = i
                break
        if bad is None:
            break
        del pts[bad]
    return pts


def _stationary_run(pts: list[tuple], threshold_m: float) -> int:
    """How many points from the front stay within threshold_m of the first one."""
    k = 1
    while k < len(pts) and haversine_m(pts[0][1], pts[0][2], pts[k][1], pts[k][2]) <= threshold_m:
        k += 1
    return k


def trim_stationary(pts: list[tuple], threshold_m: float) -> list[tuple]:
    """Drop parked lead-in and tail: an activity often starts before the car moves (sitting in the
    driveway) and ends after it stops. Leading points within threshold_m of the first point collapse to
    the last of them (same place, the latest time we know the car was still there); trailing points
    within threshold_m of the last point collapse to the first of them. Points are (ts, lat, lng) in time
    order. A drive that never leaves the threshold is returned unchanged."""
    if len(pts) < 3:
        return list(pts)
    head = _stationary_run(pts, threshold_m)
    if head == len(pts):
        return list(pts)
    tail = _stationary_run(pts[::-1], threshold_m)
    return pts[head - 1:len(pts) - tail + 1]


def moving_window(pts: list[tuple], threshold_m: float) -> tuple[datetime, datetime]:
    """(moving_start, moving_end): the span of the trimmed points."""
    t = trim_stationary(pts, threshold_m)
    return t[0][0], t[-1][0]


def drive_id_for(start_utc: datetime, end_utc: datetime) -> str:
    key = f"{start_utc:%Y-%m-%dT%H:%M:%S}|{end_utc:%Y-%m-%dT%H:%M:%S}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _local(dt: datetime, fallback: ZoneInfo) -> datetime:
    """Local wall time. A 'Z'/+00:00 stamp carries no local info, so use the fallback zone."""
    if dt.utcoffset() == timedelta(0) or dt.tzinfo is None:
        base = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return base.astimezone(fallback)
    return dt


def build_drives(acts: list[RawActivity], pts: list[RawPoint], cfg: Config) -> tuple[list[Drive], dict]:
    types = {activity_type(t) for t in cfg.drive_types}
    fallback = ZoneInfo(cfg.fallback_tz)
    pts = sorted(pts, key=lambda p: p.ts)
    pts_ts = [p.ts for p in pts]
    stats = {"candidates": 0, "dropped_points": 0, "dropped_short": 0, "outlier_points": 0}
    drives = []
    for a in acts:
        if activity_type(a.type) not in types:
            continue
        stats["candidates"] += 1
        i = bisect.bisect_left(pts_ts, a.start)
        j = bisect.bisect_right(pts_ts, a.end)
        seq = []
        if a.start_coord:
            seq.append((a.start, *a.start_coord))
        seq.extend((p.ts, p.lat, p.lng) for p in pts[i:j])
        if a.end_coord:
            seq.append((a.end, *a.end_coord))
        # Sort by time, dedupe identical consecutive fixes.
        seq.sort(key=lambda p: p[0])
        clean = []
        for p in seq:
            if clean and abs((p[0] - clean[-1][0]).total_seconds()) < 1 and p[1:] == clean[-1][1:]:
                continue
            clean.append(p)
        before = len(clean)
        clean = drop_outliers(clean)
        stats["outlier_points"] += before - len(clean)
        dist = a.distance_m if a.distance_m else path_length_m([(p[1], p[2]) for p in clean])
        if len(clean) < cfg.min_points:
            stats["dropped_points"] += 1
            continue
        if dist < cfg.min_distance_m:
            stats["dropped_short"] += 1
            continue
        s_utc = a.start.astimezone(timezone.utc).replace(tzinfo=None)
        e_utc = a.end.astimezone(timezone.utc).replace(tzinfo=None)
        loc = _local(a.start, fallback)
        m_start, m_end = moving_window(clean, cfg.stationary_m)
        drives.append(Drive(
            drive_id=drive_id_for(s_utc, e_utc),
            start_time=s_utc,
            end_time=e_utc,
            local_date=loc.date().isoformat(),
            local_start=loc.replace(tzinfo=None),
            utc_offset_min=int(loc.utcoffset().total_seconds() // 60),
            points=[(p[0].astimezone(timezone.utc).replace(tzinfo=None), p[1], p[2]) for p in clean],
            reported_distance_m=dist,
            activity_confidence=a.probability,
            moving_start=m_start.astimezone(timezone.utc).replace(tzinfo=None),
            moving_end=m_end.astimezone(timezone.utc).replace(tzinfo=None),
        ))
    return drives, stats


# ---------------------------------------------------------------- storage


def store(drives: list[Drive], con) -> dict:
    """Insert drives not already present. A drive is a duplicate if its id matches or its
    time window overlaps an existing drive by more than half of the shorter window."""
    existing = con.execute("SELECT drive_id, start_time, end_time FROM drives").fetchall()
    ids = {r[0] for r in existing}
    windows = sorted((r[1], r[2]) for r in existing)
    starts = [w[0] for w in windows]

    def overlaps(d: Drive) -> bool:
        k = bisect.bisect_left(starts, d.start_time - timedelta(hours=12))
        for s, e in windows[k:]:
            if s > d.end_time:
                break
            ov = (min(e, d.end_time) - max(s, d.start_time)).total_seconds()
            shorter = min((e - s).total_seconds(), (d.end_time - d.start_time).total_seconds()) or 1
            if ov / shorter > 0.5:
                return True
        return False

    new = [d for d in drives if d.drive_id not in ids and not overlaps(d)]
    # Also dedupe within this batch by id.
    seen, uniq = set(), []
    for d in new:
        if d.drive_id not in seen:
            seen.add(d.drive_id)
            uniq.append(d)

    if uniq:
        drives_tbl = pa.table({
            "drive_id": [d.drive_id for d in uniq],
            "start_time": [d.start_time for d in uniq],
            "end_time": [d.end_time for d in uniq],
            "local_date": [date.fromisoformat(d.local_date) for d in uniq],
            "local_start": [d.local_start for d in uniq],
            "utc_offset_min": [d.utc_offset_min for d in uniq],
            "reported_distance_m": [d.reported_distance_m for d in uniq],
            "activity_confidence": [d.activity_confidence for d in uniq],
            "n_points": [len(d.points) for d in uniq],
            "moving_start": [d.moving_start for d in uniq],
            "moving_end": [d.moving_end for d in uniq],
        })
        points_tbl = pa.table({
            "drive_id": [d.drive_id for d in uniq for _ in d.points],
            "seq": [i for d in uniq for i in range(len(d.points))],
            "ts": [p[0] for d in uniq for p in d.points],
            "lat": [p[1] for d in uniq for p in d.points],
            "lng": [p[2] for d in uniq for p in d.points],
        })
        con.register("new_drives", drives_tbl)
        con.register("new_points", points_tbl)
        con.execute("""INSERT INTO drives (drive_id, start_time, end_time, local_date, local_start,
                           utc_offset_min, reported_distance_m, activity_confidence, n_points,
                           moving_start, moving_end, match_status)
                       SELECT *, 'pending' FROM new_drives""")
        con.execute("INSERT INTO drive_points SELECT * FROM new_points")
        con.unregister("new_drives")
        con.unregister("new_points")
    return {"inserted": len(uniq), "skipped_existing": len(drives) - len(uniq)}


def run(cfg: Config | None = None) -> dict:
    cfg = cfg or load_config()
    fmt = detect_format(cfg.timeline_path)
    if fmt not in ADAPTERS:
        raise SystemExit(f"Unrecognized Timeline format in {cfg.timeline_path}")
    acts, pts = ADAPTERS[fmt](cfg.timeline_path).read()
    drives, stats = build_drives(acts, pts, cfg)
    con = connect(cfg)
    stats.update(store(drives, con))
    n, km = con.execute("SELECT count(*), sum(reported_distance_m) / 1000 FROM drives").fetchone()
    types = {activity_type(t) for t in cfg.drive_types}
    source_km = sum(a.distance_m or 0 for a in acts if activity_type(a.type) in types) / 1000
    stats.update(format=fmt, drives=n, total_km=km, source_driving_km=source_km)
    con.close()
    return stats
