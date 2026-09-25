"""When you drive (patterns) and year in review. Shared by the CLI and the viewer, so both show the
same numbers.

Times are each drive's local wall time (drives.local_start, from the UTC offset in the source data), so
a drive on a trip in another time zone lands in the hour it was there.

Miles: unfiltered, a drive's miles are its matched road length (Google's reported distance only when a
drive couldn't be matched: it's often far too low, DEBUG.md v2 Phase 4). Filtered by a road or an area, miles are the miles driven *on that road / in that area*, and a
drive counts if it touched it.
"""

from __future__ import annotations

import json

import duckdb

from .queries import DRIVE_M, M_PER_MI, _rows

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LENGTH_BINS = [0, 1, 2, 5, 10, 20, 50, 100]  # miles; the last bin is 100+


def _scope(road: str | None, area: str | None) -> tuple[str, list]:
    """SQL + params for (drive_id, mi) of the drives in scope."""
    if road:
        return (f"""SELECT drive_id, sum(length_m) / {M_PER_MI} AS mi FROM road_drives
                    WHERE road_key = ? GROUP BY drive_id""", [road])
    if area:
        return (f"""SELECT t.drive_id, sum(t.length_m * wa.frac) / {M_PER_MI} AS mi
                    FROM way_areas wa JOIN traversals t USING (way_id)
                    WHERE wa.area_id = ? GROUP BY t.drive_id""", [area])
    return f"SELECT drive_id, {DRIVE_M} / {M_PER_MI} AS mi FROM drives", []


def patterns(con, road: str | None = None, area: str | None = None) -> dict:
    scope, p = _scope(road, area)
    base = f"""SELECT d.drive_id, d.local_start, d.local_date, f.mi,
                      {DRIVE_M} / {M_PER_MI} AS drive_mi
               FROM ({scope}) f JOIN drives d USING (drive_id)"""
    grid = {"drives": [[0] * 24 for _ in range(7)], "mi": [[0.0] * 24 for _ in range(7)]}
    for dow, hour, n, miles in con.execute(f"""
            SELECT isodow(local_start) - 1, hour(local_start), count(*), sum(mi) FROM ({base}) GROUP BY 1, 2""", p).fetchall():
        grid["drives"][dow][hour] = n
        grid["mi"][dow][hour] = round(miles or 0, 1)
    months = _rows(con.execute(f"""
        WITH m AS (SELECT strftime(local_date, '%Y-%m') AS month, count(*) AS drives, sum(mi) AS mi
                   FROM ({base}) GROUP BY 1),
             span AS (SELECT range AS d FROM range(
                        (SELECT date_trunc('month', min(local_date))::TIMESTAMP FROM drives),
                        (SELECT max(local_date)::TIMESTAMP FROM drives) + INTERVAL 1 DAY, INTERVAL 1 MONTH))
        SELECT strftime(d, '%Y-%m') AS month, coalesce(m.drives, 0) AS drives, round(coalesce(m.mi, 0), 1) AS mi
        FROM span LEFT JOIN m ON m.month = strftime(d, '%Y-%m') ORDER BY 1""", p))
    lengths = []
    edges = LENGTH_BINS + [None]
    for lo, hi in zip(edges, edges[1:]):
        cond = f"drive_mi >= {lo}" + (f" AND drive_mi < {hi}" if hi is not None else "")
        n = con.execute(f"SELECT count(*) FROM ({base}) WHERE {cond}", p).fetchone()[0]
        lengths.append({"label": f"{lo}–{hi}" if hi is not None else f"{lo}+", "drives": n})
    totals = con.execute(f"SELECT count(*), sum(mi), count(DISTINCT local_date) FROM ({base})", p).fetchone()
    return {"filter": {"road": road, "area": area}, "days": DAYS, "grid": grid, "months": months,
            "lengths": lengths, "drives": totals[0], "mi": round(totals[1] or 0, 1), "distinct_days": totals[2]}


# ---------------------------------------------------------------- year in review


def _year_totals(con, year: int) -> dict:
    r = con.execute(f"""
        SELECT count(*), sum({DRIVE_M}) / {M_PER_MI},
               sum(date_diff('second', start_time, end_time)) / 3600.0, count(DISTINCT local_date)
        FROM drives WHERE year(local_date) = ?""", [year]).fetchone()
    return {"drives": r[0], "mi": round(r[1] or 0, 1), "hours": round(r[2] or 0, 1), "days": r[3]}


def years(con) -> list[int]:
    return [r[0] for r in con.execute("SELECT DISTINCT year(local_date) FROM drives ORDER BY 1").fetchall()]


def review(con, year: int) -> dict:
    cur, prev = _year_totals(con, year), _year_totals(con, year - 1)
    change = {k: (None if not prev[k] else round((cur[k] - prev[k]) / prev[k], 3)) for k in cur}
    new = con.execute(f"""
        SELECT count(*) FROM road_first WHERE year(local_start) = ?""", [year]).fetchone()[0]
    # New miles: the full driven length of every way first driven this year.
    new_mi = con.execute(f"""
        SELECT sum(len) / {M_PER_MI} FROM (
            SELECT wf.way_id, max(t.length_m) AS len FROM way_first wf JOIN traversals t USING (way_id)
            JOIN drives d ON d.drive_id = wf.drive_id
            WHERE year(d.local_date) = ? GROUP BY wf.way_id)""", [year]).fetchone()[0]
    top = _rows(con.execute(f"""
        SELECT r.road_key, r.label, count(DISTINCT rd.drive_id) AS drive_count,
               sum(rd.length_m) / {M_PER_MI} AS mi
        FROM road_drives rd JOIN roads r USING (road_key) WHERE year(rd.date) = ?
        GROUP BY ALL ORDER BY drive_count DESC, mi DESC LIMIT 10""", [year]))
    busiest_month = con.execute(f"""
        SELECT strftime(local_date, '%Y-%m'), count(*), sum({DRIVE_M}) / {M_PER_MI} AS mi
        FROM drives WHERE year(local_date) = ? GROUP BY 1 ORDER BY mi DESC LIMIT 1""", [year]).fetchone()
    busiest_day = con.execute(f"""
        SELECT local_date, count(*), sum({DRIVE_M}) / {M_PER_MI} AS mi
        FROM drives WHERE year(local_date) = ? GROUP BY 1 ORDER BY mi DESC LIMIT 1""", [year]).fetchone()
    longest = con.execute(f"""
        SELECT drive_id, local_date, strftime(local_start, '%H:%M'), {DRIVE_M} / {M_PER_MI},
               date_diff('minute', start_time, end_time), match_status
        FROM drives WHERE year(local_date) = ? ORDER BY {DRIVE_M} DESC LIMIT 1""", [year]).fetchone()
    from .areas import areas_driven

    try:
        places = areas_driven(con, ("state", "country"), f"{year}-01-01", f"{year}-12-31")
    except duckdb.CatalogException:  # `drivelog areas build` hasn't been run
        places = None
    return {
        "year": year, "totals": cur, "previous": prev, "change": change,
        "new_roads": {"count": new, "mi": round(new_mi or 0, 1)},
        "top_roads": top,
        "busiest_month": busiest_month and {"month": busiest_month[0], "drives": busiest_month[1],
                                            "mi": round(busiest_month[2], 1)},
        "busiest_day": busiest_day and {"date": busiest_day[0], "drives": busiest_day[1], "mi": round(busiest_day[2], 1)},
        "longest_drive": longest and {"drive_id": longest[0], "date": longest[1], "start": longest[2],
                                      "mi": round(longest[3], 1), "minutes": longest[4], "status": longest[5]},
        "places": places,
    }


def new_roads_geojson(con, year: int) -> dict:
    """Every way first driven in the year, as one map layer."""
    rows = con.execute("""
        SELECT w.way_id, w.geometry, coalesce(w.name, w.ref)
        FROM way_first wf JOIN drives d ON d.drive_id = wf.drive_id JOIN way_info w USING (way_id)
        WHERE year(d.local_date) = ?""", [year]).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"way_id": str(wid), "name": nm, "inferred": False, "confidence": "observed"},
         "geometry": {"type": "LineString", "coordinates": json.loads(g)}} for wid, g, nm in rows]}
