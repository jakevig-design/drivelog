"""drivelog.quick: places from GPS points alone (invented squares and coordinates, no real data)."""

from datetime import datetime, timedelta

import duckdb

from drivelog.facets import Engine
from drivelog.quick import places_from_points


def _sq(x0, y0, x1, y1):
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def test_places_from_points_credits_each_step_to_the_area_of_its_midpoint():
    c = duckdb.connect()
    c.execute("INSTALL spatial; LOAD spatial")
    c.execute("""CREATE TABLE areas (area_id VARCHAR, kind VARCHAR, name VARCHAR, admin_level INTEGER, county VARCHAR,
                 state_code VARCHAR, state_name VARCHAR, country VARCHAR, source VARCHAR, geom GEOMETRY)""")
    for aid, kind, name, poly in [
        ("ne0:XX", "country", "Xland", _sq(0, 0, 1, 1)),
        ("ne1:A", "state", "Alphastate", _sq(0, 0, 0.5, 1)),
        ("ne1:B", "state", "Betastate", _sq(0.5, 0, 1, 1)),
        ("cplace:1", "locality", "Wexbury", _sq(0.1, 0.1, 0.2, 0.2)),
    ]:
        c.execute("INSERT INTO areas VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 'XX', 'test', ST_GeomFromText(?))",
                  [aid, kind, name, poly])
    c.execute("CREATE TABLE drive_points (drive_id VARCHAR, seq INTEGER, ts TIMESTAMP, lat DOUBLE, lng DOUBLE)")
    t = datetime(1996, 3, 4, 8)
    # Drive "x": inside Wexbury (state A), then east across into state B. Drive "y": a 50 m hop (too short).
    pts = {"x": [(0.15, 0.12), (0.15, 0.18), (0.15, 0.40), (0.15, 0.70)], "y": [(0.3, 0.3), (0.3, 0.3005)]}
    for d, ps in pts.items():
        for i, (lat, lng) in enumerate(ps):
            c.execute("INSERT INTO drive_points VALUES (?, ?, ?, ?, ?)", [d, i, t + timedelta(minutes=i), lat, lng])
    places_from_points(c)
    got = {(d, a) for d, a, _ in c.execute("SELECT * FROM drive_places").fetchall()}
    assert got == {("x", "ne0:XX"), ("x", "ne1:A"), ("x", "ne1:B"), ("x", "cplace:1")}
    # Wexbury gets only the first step (0.06 deg of longitude ~ 6.7 km), not the whole drive.
    m = c.execute("SELECT m FROM drive_places WHERE area_id = 'cplace:1'").fetchone()[0]
    assert 6000 < m < 7500


def test_engine_runs_in_quick_mode_on_point_path_miles():
    c = duckdb.connect()
    c.execute("""CREATE TABLE drives (drive_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, local_date DATE,
                 local_start TIMESTAMP, reported_distance_m DOUBLE, match_status VARCHAR, matched_length_m DOUBLE,
                 path_m DOUBLE)""")
    t = datetime(1996, 3, 4, 8)
    c.execute("INSERT INTO drives VALUES ('x', ?, ?, ?, ?, 99999, 'pending', NULL, 16093.44)", [t, t + timedelta(minutes=20), t.date(), t])
    e = Engine(c)
    r = e.query({})
    assert e.mode == "quick" and r["totals"]["mi"] == 10.0   # path_m, not Google's reported distance
    assert "road" not in r["facets"]
