"""Time patterns and year in review on a tiny synthetic history."""

import json
from datetime import date, datetime, timedelta

import duckdb
import pytest

from drivelog import patterns

MI = 1609.344


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("""CREATE TABLE drives (drive_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, local_date DATE,
                 local_start TIMESTAMP, utc_offset_min INTEGER, reported_distance_m DOUBLE, match_status VARCHAR,
                 matched_length_m DOUBLE)""")
    # (id, local start, offset min, miles). d3 is on a trip 3 time zones west: its local hour is 7,
    # though it's 10:00 back home.
    drives = [("d1", datetime(1996, 3, 4, 8, 15), -300, 10), ("d2", datetime(1996, 3, 4, 17, 40), -300, 3),
              ("d3", datetime(1997, 6, 7, 7, 5), -420, 120), ("d4", datetime(1997, 6, 8, 23, 30), -240, 0.5),
              ("d5", datetime(1997, 12, 31, 8, 0), -300, 30)]
    for did, local, off, miles in drives:
        utc = local - timedelta(minutes=off)
        # Matched length is the real distance; the reported one is half of it (as Timeline often does).
        c.execute("INSERT INTO drives VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
                  [did, utc, utc + timedelta(minutes=30), local.date(), local, off, miles * MI / 2, miles * MI])
    c.execute("CREATE TABLE road_drives (road_key VARCHAR, drive_id VARCHAR, date DATE, local_start TIMESTAMP, length_m DOUBLE)")
    c.execute("CREATE TABLE traversals (drive_id VARCHAR, way_id BIGINT, date DATE, local_start TIMESTAMP, length_m DOUBLE)")
    c.execute("CREATE TABLE roads (road_key VARCHAR, label VARCHAR, ref VARCHAR, names VARCHAR[])")
    c.execute("CREATE TABLE way_info (way_id BIGINT, name VARCHAR, ref VARCHAR, road_class VARCHAR, geometry VARCHAR)")
    for wid, key, did, miles in [(1, "XX 9", "d1", 4), (1, "XX 9", "d5", 4), (2, "Elm Road", "d3", 50), (1, "XX 9", "d2", 2)]:
        d = c.execute("SELECT local_date, local_start FROM drives WHERE drive_id = ?", [did]).fetchone()
        c.execute("INSERT INTO road_drives VALUES (?, ?, ?, ?, ?)", [key, did, d[0], d[1], miles * MI])
        c.execute("INSERT INTO traversals VALUES (?, ?, ?, ?, ?)", [did, wid, d[0], d[1], miles * MI])
    c.execute("INSERT INTO roads VALUES ('XX 9', 'XX 9', 'XX 9', []), ('Elm Road', 'Elm Road', NULL, [])")
    c.execute("INSERT INTO way_info VALUES (1, NULL, 'XX 9', 'primary', ?), (2, 'Elm Road', NULL, 'primary', ?)",
              [json.dumps([[0, 0], [0, 1]]), json.dumps([[1, 1], [1, 2]])])
    c.execute("CREATE TABLE road_first AS SELECT road_key, arg_min(drive_id, local_start) AS drive_id, min(local_start) AS local_start FROM road_drives GROUP BY 1")
    c.execute("CREATE TABLE way_first AS SELECT way_id, arg_min(drive_id, local_start) AS drive_id FROM traversals GROUP BY 1")
    return c


def test_grid_uses_local_time(con):
    p = patterns.patterns(con)
    g = p["grid"]["drives"]
    assert g[0][8] == 1 and g[0][17] == 1     # d1, d2: Monday
    assert g[5][7] == 1                       # d3: Saturday at 07:00 local, not 10:00
    assert g[6][23] == 1                      # d4: Sunday 23:30
    assert p["drives"] == 5 and sum(map(sum, g)) == 5
    assert p["months"][0]["month"] == "1996-03" and p["months"][-1]["month"] == "1997-12"
    assert len(p["months"]) == 22             # every month in the span, empty ones included
    assert [b["drives"] for b in p["lengths"]] == [1, 0, 1, 0, 1, 1, 0, 1]  # 0.5, 3, 10, 30, 120 mi


def test_patterns_filtered_by_road(con):
    p = patterns.patterns(con, road="XX 9")
    assert p["drives"] == 3 and p["mi"] == pytest.approx(10.0)
    assert p["grid"]["mi"][0][8] == pytest.approx(4.0)  # miles on the road, not the whole drive


def test_review_totals_new_roads_and_comparison(con):
    r = patterns.review(con, 1997)
    assert r["totals"]["drives"] == 3 and r["totals"]["mi"] == pytest.approx(150.5)
    assert r["previous"]["drives"] == 2 and r["change"]["drives"] == pytest.approx(0.5)
    assert r["new_roads"] == {"count": 1, "mi": pytest.approx(50.0)}   # Elm Road; XX 9 was new in 1996
    assert r["longest_drive"]["drive_id"] == "d3" and r["busiest_day"]["date"] == date(1997, 6, 7)
    assert r["top_roads"][0]["label"] in ("XX 9", "Elm Road")
    assert r["places"] is None                                          # areas not built in this db
    assert len(patterns.new_roads_geojson(con, 1997)["features"]) == 1
    assert patterns.years(con) == [1996, 1997]
