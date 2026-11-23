"""Fill the gaps between observed segments of each drive, and finalize match status.

Timeline exports thin the GPS track, so consecutive points can be many km apart. For each gap we ask
Valhalla for the fastest route plus alternates and pick the candidate that best fits:

  * time: the route's drive time vs. the time that actually elapsed between the two points, and
  * habit: the share of the route on roads this history has *observed* you on before.

Every fill edge is stored with inferred = true and a confidence:
  high  the gap is short (< SHORT_GAP_M); or the chosen route's time fits the elapsed time; or the
        route is mostly on roads you're observed on (>= HABIT_HIGH) and its time is at least plausible
  low   anything else (time unknown on an unfamiliar route, or a clear time mismatch)

Route times are scaled by the driver's *pace*: the median elapsed / route-time ratio over past fills.
Valhalla's default speeds run slow for most drivers (0.77 here; DEBUG.md, v2 Phase 1), so an unscaled
comparison flags ordinary drives on familiar roads as too fast. Timestamps are minute-rounded, so the
bands allow ±ROUNDING_S; a route that would need more than MAX_SPEED_MS is never high confidence.

Timing uses each drive's *moving window* (drives.moving_start / moving_end): a gap that starts while
the car is still parked in the driveway is timed from when it started moving, and one that ends after
it parked is timed to when it stopped. The original activity times stay in drives.start_time/end_time.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import duckdb
import httpx

from .config import Config, load_config
from .coverage import load_coverage
from .db import connect
from .match import GAP_OFFSET, Edge, Gap, Matcher, edges_table, merge_edges

SHORT_GAP_M = 1500
TIME_FIT = (0.7, 1.6)      # elapsed / route_time within this band = the route explains the gap
TIME_PLAUSIBLE = (0.5, 2.5)  # looser band, accepted when the route is on familiar roads
HABIT_HIGH = 0.8
HABIT_WEIGHT = 0.7         # how much familiarity can outweigh time fit when choosing a route
ALTERNATES = 2
ROUNDING_S = 60            # Timeline path times are minute-rounded
MAX_SPEED_MS = 40.2        # 90 mph average along the route: faster means the timestamps are wrong
PACE_MIN_SAMPLES = 200


@dataclass
class Fill:
    edges: list[Edge] = field(default_factory=list)
    route_m: float = 0.0
    route_s: float | None = None
    elapsed_s: float | None = None
    candidates: int = 0
    habit_share: float | None = None
    confidence: str = "low"


def load_habit(con) -> set[int]:
    """Ways observed (not inferred) on any drive."""
    return {r[0] for r in con.execute(
        "SELECT DISTINCT way_id FROM drive_edges WHERE NOT inferred AND way_id IS NOT NULL").fetchall()}


Window = tuple[datetime, datetime] | None  # (moving_start, moving_end), tz-aware UTC


def elapsed_s(gap: Gap, window: Window = None) -> float | None:
    """Seconds between the gap's two trace times, with both clamped to the drive's moving window."""
    if not (gap.a_ts and gap.b_ts):
        return None
    a, b = gap.a_ts, gap.b_ts
    if window:
        a, b = max(a, window[0]), min(b, window[1])
    return (b - a).total_seconds() if b > a else None


def measure_pace(con) -> float | None:
    """Median elapsed / Valhalla route time over timed gaps > SHORT_GAP_M from the last fill."""
    n, pace = con.execute(f"""SELECT count(*), median(elapsed_s / route_s) FROM drive_gaps
                              WHERE filled AND elapsed_s > 0 AND route_s > 0 AND straight_m > {SHORT_GAP_M}""").fetchone()
    return float(pace) if n >= PACE_MIN_SAMPLES and pace else None


def load_pace(con) -> float:
    """The pace measured on the current gaps, else the one saved by the last fill (a rematch clears the
    gaps before filling), else 1.0: the first fill on a new database is unscaled; a refill calibrates."""
    pace = measure_pace(con)
    if pace is None:
        try:
            r = con.execute("SELECT value FROM meta WHERE key = 'fill_pace'").fetchone()
            pace = float(r[0]) if r else None
        except duckdb.CatalogException:
            pace = None
    return pace or 1.0


def save_pace(con) -> None:
    pace = measure_pace(con)
    if pace is not None:
        con.execute("CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('fill_pace', ?)", [str(pace)])


def _in_band(elapsed: float, expected: float, band: tuple[float, float]) -> bool:
    """Does any elapsed within the rounding error fall in band × expected?"""
    lo, hi = max(elapsed - ROUNDING_S, 1) / expected, (elapsed + ROUNDING_S) / expected
    return hi >= band[0] and lo <= band[1]


def rate(straight_m: float, route_m: float, route_s: float | None, elapsed: float | None,
         habit_share: float, pace: float = 1.0) -> str:
    """Confidence of a chosen fill route: 'high' or 'low' (rules in the module docstring)."""
    if straight_m < SHORT_GAP_M:
        return "high"
    timed = bool(elapsed and route_s)
    if timed and route_m / (elapsed + ROUNDING_S) > MAX_SPEED_MS:
        return "low"
    expected = route_s * pace if timed else None
    fits = timed and _in_band(elapsed, expected, TIME_FIT)
    familiar = habit_share >= HABIT_HIGH and (not timed or _in_band(elapsed, expected, TIME_PLAUSIBLE))
    return "high" if fits or familiar else "low"


def fill_gap(m: Matcher, habit: set[int], gap: Gap, window: Window = None, pace: float = 1.0) -> Fill:
    out = Fill()
    out.elapsed_s = elapsed_s(gap, window)
    try:
        r = m.post(m.base + "/route", {
            "locations": [{"lat": gap.a[1], "lon": gap.a[0]}, {"lat": gap.b[1], "lon": gap.b[0]}],
            "costing": "auto", "directions_type": "none", "alternates": ALTERNATES})
        if r.status_code != 200:
            return out
        data = r.json()
        trips = [data["trip"]] + [a["trip"] for a in data.get("alternates", []) if "trip" in a]
    except (httpx.HTTPError, KeyError, ValueError):
        return out

    fastest = min(t["summary"]["time"] for t in trips) or 1.0
    best, best_score = None, math.inf
    for t in trips:
        try:
            edges = m.edge_walk(t["legs"][0]["shape"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            continue
        length = sum(e.length_m for e in edges)
        if not edges or length <= 0:
            continue
        habit_share = sum(e.length_m for e in edges if e.way_id in habit) / length
        route_s = t["summary"]["time"]
        if out.elapsed_s and route_s > 0:
            fit = abs(math.log(max(out.elapsed_s, 1) / (route_s * pace)))
        else:
            fit = route_s / fastest - 1  # no timing: prefer the fastest, bent by familiarity
        score = fit - HABIT_WEIGHT * habit_share
        out.candidates += 1
        if score < best_score:
            best_score = score
            best = (edges, length, route_s, habit_share)
    if best is None:
        return out
    edges, out.route_m, out.route_s, out.habit_share = best
    out.confidence = rate(gap.straight_m, out.route_m, out.route_s, out.elapsed_s, out.habit_share, pace)
    for e in edges:
        e.inferred, e.confidence = True, out.confidence
    out.edges = merge_edges(edges)
    return out


def finalize(detail: dict, observed_m: float, filled_m: float, low_m: float,
             reported_m: float | None) -> tuple[str, float | None, dict]:
    total = observed_m + filled_m
    ratio = total / reported_m if reported_m else None
    detail = dict(detail, gap_filled_km=round(filled_m / 1000, 3), low_conf_km=round(low_m / 1000, 3),
                  length_ratio=round(ratio, 3) if ratio is not None else None)
    if total <= 0:
        return "failed", 0.0, detail
    share = detail.get("point_share") or 0.0
    score = round(share * (min(ratio, 1 / ratio) if ratio else 0.5), 3)
    partial = (detail.get("dropped_uncovered", 0) > 0 or detail.get("failed_pieces", 0) > 0
               or detail.get("split_used") or filled_m / total > 0.5 or low_m / total > 0.25
               or ratio is None or not (0.5 <= ratio <= 2.0))
    return ("partial" if partial else "ok"), score, detail


def _gap_from_row(row) -> Gap:
    _, _, a_lng, a_lat, a_ts, b_lng, b_lat, b_ts = row[:8]
    from datetime import timezone

    tz = lambda t: t.replace(tzinfo=timezone.utc) if t else None  # noqa: E731
    return Gap([a_lng, a_lat], [b_lng, b_lat], tz(a_ts), tz(b_ts))


def fill_drive(m: Matcher, habit: set[int], gaps: list[tuple[int, Gap]],
               window: Window = None, pace: float = 1.0) -> list[tuple[int, Fill]]:
    return [(seq, fill_gap(m, habit, g, window, pace)) for seq, g in gaps]


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t else None


def update_moving_windows(con, drive_ids: list[str], threshold_m: float) -> None:
    """(Re)compute moving_start / moving_end from each drive's points (cheap; run before every fill so
    a changed [fill] stationary_m in config.toml takes effect on the next refill)."""
    import pyarrow as pa

    from .extract import moving_window

    rows = con.execute("""SELECT drive_id, ts, lat, lng FROM drive_points WHERE drive_id IN (SELECT unnest(?))
                          ORDER BY drive_id, seq""", [drive_ids]).fetchall()
    pts: dict[str, list] = {}
    for d, ts, lat, lng in rows:
        pts.setdefault(d, []).append((ts, lat, lng))
    ids = list(pts)
    wins = [moving_window(pts[d], threshold_m) for d in ids]
    con.register("_mw", pa.table({"drive_id": ids, "ms": [w[0] for w in wins], "me": [w[1] for w in wins]}))
    con.execute("""UPDATE drives SET moving_start = _mw.ms, moving_end = _mw.me FROM _mw
                   WHERE drives.drive_id = _mw.drive_id""")
    con.unregister("_mw")


def run(drive_ids: list[str] | None = None, refill: bool = False, cfg: Config | None = None,
        progress: bool = True) -> dict:
    """Fill gaps and finalize status for drives left 'unfilled' by matching (or all, with refill)."""
    cfg = cfg or load_config()
    con = connect(cfg)
    if drive_ids is None:
        where = "match_status IN ('unfilled', 'ok', 'partial')" if refill else "match_status = 'unfilled'"
        drive_ids = [r[0] for r in con.execute(f"SELECT drive_id FROM drives WHERE {where}").fetchall()]
    if not drive_ids:
        con.close()
        return {}
    habit = load_habit(con)
    pace = load_pace(con)
    if progress:
        print(f"  pace {pace:.3f} (elapsed / Valhalla route time)", flush=True)
    m = Matcher(cfg, load_coverage(cfg))
    update_moving_windows(con, drive_ids, cfg.stationary_m)
    info = {r[0]: r[1:] for r in con.execute(
        """SELECT drive_id, reported_distance_m, match_detail, moving_start, moving_end FROM drives
           WHERE drive_id IN (SELECT unnest(?))""", [drive_ids]).fetchall()}
    # Drives split after a hidden stop start at an estimated departure time (drivelog.split): a gap
    # timed from it would fit by construction, so it's treated as untimed.
    est_first = dict(con.execute(
        """SELECT d.drive_id, min(p.ts) FROM drives d JOIN drive_points p USING (drive_id)
           WHERE d.est_start AND d.drive_id IN (SELECT unnest(?)) GROUP BY 1""", [drive_ids]).fetchall())
    counts: dict[str, int] = {}
    batch = 300
    for start in range(0, len(drive_ids), batch):
        ids = drive_ids[start:start + batch]
        gap_rows: dict[str, list] = {}
        for row in con.execute("""SELECT drive_id, seq, a_lng, a_lat, a_ts, b_lng, b_lat, b_ts FROM drive_gaps
                                  WHERE drive_id IN (SELECT unnest(?)) ORDER BY drive_id, seq""", [ids]).fetchall():
            g = _gap_from_row(row)
            if row[0] in est_first and row[4] == est_first[row[0]]:
                g.a_ts = None
            gap_rows.setdefault(row[0], []).append((row[1], g))
        results: dict[str, list] = {}
        with ThreadPoolExecutor(cfg.valhalla.get("workers", 4)) as pool:
            futs = {pool.submit(fill_drive, m, habit, gap_rows[d], (_utc(info[d][2]), _utc(info[d][3])), pace): d
                    for d in ids if d in gap_rows}
            for f in as_completed(futs):
                results[futs[f]] = f.result()

        observed = dict(con.execute("""SELECT drive_id, sum(length_m) FROM drive_edges
                                       WHERE NOT inferred AND drive_id IN (SELECT unnest(?)) GROUP BY 1""",
                                    [ids]).fetchall())
        edge_rows, gap_updates, drive_updates = [], [], []
        for d in ids:
            fills = results.get(d, [])
            for seq, f in fills:
                edge_rows.extend((d, seq + j, e) for j, e in enumerate(f.edges[:GAP_OFFSET - 1]))
                gap_updates.append((bool(f.edges), f.route_m, f.route_s, f.elapsed_s, f.candidates,
                                    f.habit_share, f.confidence if f.edges else None, d, seq))
            filled_m = sum(f.route_m for _, f in fills if f.edges)
            low_m = sum(f.route_m for _, f in fills if f.edges and f.confidence == "low")
            reported, detail = info[d][:2]
            status, score, detail = finalize(json.loads(detail or "{}"), observed.get(d) or 0.0,
                                             filled_m, low_m, reported)
            drive_updates.append((status, score, (observed.get(d) or 0.0) + filled_m, json.dumps(detail), d))
            counts[status] = counts.get(status, 0) + 1

        con.execute("BEGIN")
        con.execute("DELETE FROM drive_edges WHERE inferred AND drive_id IN (SELECT unnest(?))", [ids])
        if edge_rows:
            con.register("_fill", edges_table(edge_rows))
            con.execute("INSERT INTO drive_edges SELECT * FROM _fill")
            con.unregister("_fill")
        if gap_updates:
            con.executemany("""UPDATE drive_gaps SET filled = ?, route_m = ?, route_s = ?, elapsed_s = ?,
                               candidates = ?, habit_share = ?, confidence = ? WHERE drive_id = ? AND seq = ?""",
                            gap_updates)
        con.executemany("""UPDATE drives SET match_status = ?, match_score = ?, matched_length_m = ?,
                           match_detail = ? WHERE drive_id = ?""", drive_updates)
        con.execute("COMMIT")
        if progress:
            print(f"  filled {min(start + batch, len(drive_ids)):,}/{len(drive_ids):,}  {counts}", flush=True)
    save_pace(con)
    con.close()
    return counts
