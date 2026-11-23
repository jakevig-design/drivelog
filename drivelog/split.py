"""Split drive activities that the export stitched together across a stop.

Timeline sometimes records a whole day as one driving activity (a "drive" of 15+ hours, say).
A drive is split wherever the car was stopped for STOP_S or more inside it. Two kinds of stop count:

  observed  consecutive points that stay within [fill] stationary_m of the first of them for STOP_S
            or more (the car is seen standing still).
  hidden    two consecutive points further apart than that, where the time between them exceeds the
            time the drive between them would take by STOP_S or more. "Would take" is Valhalla's route
            time scaled by the driver's pace, times the top of the fill's time-fit band
            (drivelog.fill.TIME_FIT), so ordinary traffic doesn't read as a stop.

Both must exceed STOP_S after allowing ROUNDING_S for Timeline's minute-rounded timestamps.

A stop at the start or end of a drive (a parked lead-in or tail) trims it instead: the outer piece is
only parked points and is dropped as too short, so the drive keeps its moving part. The moving window
already kept that wait out of route timing, but drive minutes and "longest drive" counted it (DEBUG.md,
v3 Phase 2).

Where a hidden stop happened along its gap isn't known. The rule used everywhere: the car waited at the
earlier point, then drove to the later one at its usual pace. So the piece after a hidden stop starts at
the earlier point's position, stamped with the estimated departure (later time - pace * route time), and
is flagged est_start: drivelog.fill then treats that first gap as untimed, so the made-up time can't make
the route look confirmed. An observed stop ends the earlier piece at the first stationary point (arrival)
and starts the next at the last one (the latest time the car was seen there), as trim_stationary does.

Pieces shorter than [extract] min_distance_m, or with fewer than 2 points, are dropped like any other
short activity. Each piece gets drive_id_for(start, end) and drives.split_from = the original id, so a
later re-extract of the same export sees the stitched activity as a duplicate of its pieces (>50% overlap)
and doesn't bring it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .config import Config, load_config
from .db import connect
from .extract import drive_id_for
from .fill import ROUNDING_S, TIME_FIT
from .geo import haversine_m, path_length_m

STOP_S = 600            # 10 minutes: chosen from the data (DECISIONS.md, v3 Phase 2)
FALLBACK_SPEED_MS = 11  # ~25 mph along a 1.4x-detoured straight line, when Valhalla has no route
FALLBACK_DETOUR = 1.4

Point = tuple  # (ts: naive UTC datetime, lat, lng)
RouteFn = Callable[[Point, Point], "float | None"]  # route time in seconds, or None


@dataclass
class Stop:
    kind: str            # observed | hidden
    i: int               # last point index of the piece before the stop
    j: int               # first point index of the piece after it
    seconds: float       # how long the car was stopped (hidden: the time beyond the allowance)
    depart: datetime | None = None  # hidden: estimated departure from point i


def _allowance(a: Point, b: Point, route_s: float | None, pace: float) -> tuple[float, float]:
    """(seconds the a -> b drive may take before the excess counts as a stop, expected drive time)."""
    if route_s is None:
        expected = haversine_m(a[1], a[2], b[1], b[2]) * FALLBACK_DETOUR / FALLBACK_SPEED_MS
    else:
        expected = route_s * pace
    return TIME_FIT[1] * expected, expected


def find_stops(pts: list[Point], route: RouteFn, stationary_m: float, pace: float,
               stop_s: float = STOP_S) -> list[Stop]:
    n = len(pts)
    stops: list[Stop] = []
    k = 0
    while k < n - 1:
        # Observed: points staying within stationary_m of pts[k].
        j = k
        while j + 1 < n and haversine_m(pts[k][1], pts[k][2], pts[j + 1][1], pts[j + 1][2]) <= stationary_m:
            j += 1
        if j > k:
            span = (pts[j][0] - pts[k][0]).total_seconds()
            if span - ROUNDING_S >= stop_s:
                stops.append(Stop("observed", k, j, span))
            k = j
            continue
        # Hidden: more time between two points than driving between them explains.
        a, b = pts[k], pts[k + 1]
        dt = (b[0] - a[0]).total_seconds()
        if dt >= stop_s:
            allow, expected = _allowance(a, b, route(a, b), pace)
            if dt - ROUNDING_S - allow >= stop_s:
                depart = max(a[0], b[0] - timedelta(seconds=expected))
                stops.append(Stop("hidden", k, k + 1, dt - allow, depart))
        k += 1
    return stops


@dataclass
class Piece:
    points: list[Point]
    est_start: bool = False
    first: bool = False  # holds the drive's first point: keeps the activity's start time
    last: bool = False   # holds its last point: keeps the activity's end time

    @property
    def length_m(self) -> float:
        return path_length_m([(p[1], p[2]) for p in self.points])


def split_points(pts: list[Point], stops: list[Stop]) -> list[Piece]:
    """All pieces, before the length filter. Without stops: one piece, the drive itself."""
    pieces: list[Piece] = []
    cur: list[Point] = []
    est = False
    k = 0
    for s in stops:
        cur.extend(pts[k:s.i + 1])
        pieces.append(Piece(cur, est))
        if s.kind == "observed":
            cur, est, k = [], False, s.j
        else:
            a = pts[s.i]
            cur, est, k = [(s.depart, a[1], a[2])], True, s.j
    cur.extend(pts[k:])
    pieces.append(Piece(cur, est))
    pieces[0].first = not pieces[0].est_start
    pieces[-1].last = True
    return pieces


def keep(pieces: list[Piece], min_points: int, min_distance_m: float) -> list[Piece]:
    """Pieces long enough to be drives. If none is, the longest piece that moves at all: the activity
    was already kept as a drive, and its stops still come off. A drive whose points never move has no
    piece left and stays as recorded."""
    ok = [p for p in pieces if len(p.points) >= max(2, min_points) and p.length_m >= min_distance_m]
    if ok:
        return ok
    moving = [p for p in pieces if len(p.points) >= 2 and p.length_m > 0]
    return [max(moving, key=lambda p: p.length_m)] if moving else []


# ---------------------------------------------------------------- database


def valhalla_route(cfg: Config) -> RouteFn:
    import httpx

    base = cfg.valhalla.get("url", "http://localhost:8002")
    client = httpx.Client(timeout=60)

    def route(a: Point, b: Point) -> float | None:
        try:
            r = client.post(f"{base}/route", json={
                "locations": [{"lat": a[1], "lon": a[2]}, {"lat": b[1], "lon": b[2]}],
                "costing": "auto", "directions_type": "none"})
            return r.json()["trip"]["summary"]["time"] if r.status_code == 200 else None
        except (httpx.HTTPError, KeyError, ValueError):
            return None

    return route


def plan(con, cfg: Config, route: RouteFn, drive_ids: list[str] | None = None, pace: float | None = None) -> dict:
    """{drive_id: (stops, all pieces, kept pieces)} for every drive with at least one stop."""
    from .fill import load_pace

    pace = pace if pace is not None else load_pace(con)
    where = "" if drive_ids is None else "WHERE drive_id IN (SELECT unnest(?))"
    rows = con.execute(f"SELECT drive_id, ts, lat, lng FROM drive_points {where} ORDER BY drive_id, seq",
                       [] if drive_ids is None else [drive_ids]).fetchall()
    pts: dict[str, list] = {}
    for d, ts, lat, lng in rows:
        pts.setdefault(d, []).append((ts, lat, lng))
    out = {}
    for d, p in pts.items():
        stops = find_stops(p, route, cfg.stationary_m, pace)
        if stops:
            pieces = split_points(p, stops)
            out[d] = (stops, pieces, keep(pieces, cfg.min_points, cfg.min_distance_m))
    return out


def apply(con, plans: dict) -> list[str]:
    """Replace each planned drive with its pieces (match_status pending). Returns the new drive ids.
    A drive whose pieces are all too short is left as it was."""
    import pyarrow as pa

    if not plans:
        return []
    info = {r[0]: r[1:] for r in con.execute(
        """SELECT drive_id, start_time, end_time, utc_offset_min, reported_distance_m, activity_confidence
           FROM drives WHERE drive_id IN (SELECT unnest(?))""", [list(plans)]).fetchall()}
    new_drives, new_points, replaced = [], [], []
    for d, (_, everything, pieces) in plans.items():
        if not pieces:
            continue
        start, end, off, reported, conf = info[d]
        all_len = sum(p.length_m for p in everything) or 1.0
        for p in pieces:
            s = start if p.first else p.points[0][0]
            e = end if p.last else p.points[-1][0]
            pid = drive_id_for(s, e)
            loc = s + timedelta(minutes=off or 0)
            new_drives.append(dict(
                drive_id=pid, start_time=s, end_time=e, local_date=loc.date(), local_start=loc,
                utc_offset_min=off, reported_distance_m=reported * p.length_m / all_len if reported else None,
                activity_confidence=conf, n_points=len(p.points), split_from=d, est_start=p.est_start))
            new_points.extend((pid, i, *pt) for i, pt in enumerate(p.points))
        replaced.append(d)
    if not new_drives:
        return []
    cols = list(new_drives[0])
    con.execute("BEGIN")
    for t in ("drive_edges", "drive_gaps", "drive_points", "drives"):
        con.execute(f"DELETE FROM {t} WHERE drive_id IN (SELECT unnest(?))", [replaced])
    con.register("_nd", pa.table({c: [r[c] for r in new_drives] for c in cols}))
    con.execute(f"INSERT INTO drives ({', '.join(cols)}, match_status) SELECT *, 'pending' FROM _nd")
    con.unregister("_nd")
    con.register("_np", pa.table({c: [r[i] for r in new_points]
                                  for i, c in enumerate(["drive_id", "seq", "ts", "lat", "lng"])}))
    con.execute("INSERT INTO drive_points SELECT * FROM _np")
    con.unregister("_np")
    con.execute("COMMIT")
    return [r["drive_id"] for r in new_drives]


def report(plans: dict, applied: set[str]) -> dict:
    split = [d for d in plans if d in applied]
    multi = [d for d in split if len(plans[d][2]) > 1]
    return {
        "activities_with_stops": len(plans),
        "split": len(split),
        "into_drives": sum(len(plans[d][2]) for d in split),
        "split_into_several": len(multi),         # became 2+ drives
        "several_into": sum(len(plans[d][2]) for d in multi),
        "trimmed_to_one": len(split) - len(multi),  # one drive left: the stop was at an edge, or the
                                                    # other side was too short to keep
        "left_whole": len(plans) - len(split),  # every piece too short
        "stops": {k: sum(1 for d in plans for s in plans[d][0] if s.kind == k) for k in ("observed", "hidden")},
    }


def run(cfg: Config | None = None, drive_ids: list[str] | None = None, match_after: bool = True,
        progress: bool = True, dry_run: bool = False) -> dict:
    """Find stitched drives (all, or just drive_ids), replace them with their pieces, then match and
    fill only the new pieces and rebuild the road tables. dry_run reports what would change."""
    cfg = cfg or load_config()
    con = connect(cfg, read_only=dry_run)
    plans = plan(con, cfg, valhalla_route(cfg), drive_ids)
    if dry_run:
        con.close()
        return report(plans, {d for d, p in plans.items() if p[2]}) | {"dry_run": True}
    before = {d for d, p in plans.items() if p[2]}
    new_ids = apply(con, plans)
    con.close()
    out = report(plans, before)
    if progress:
        print(f"  split {out['split']:,} activities into {out['into_drives']:,} drives "
              f"({out['stops']['observed']} observed + {out['stops']['hidden']} hidden stops)", flush=True)
    if match_after and new_ids:
        from . import aggregate, areas, match

        out["match"] = match.run(drive_ids=new_ids, cfg=cfg, progress=progress)
        out["aggregate"] = aggregate.run(cfg)
        con = connect(cfg)
        if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'areas'").fetchone()[0]:
            areas.assign_ways(con)
        con.close()
    return out
