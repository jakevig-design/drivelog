"""drivelog.quality: flights and impossible records are set aside; real drives never are."""

from datetime import datetime, timedelta

import duckdb

from drivelog.quality import find, set_aside

T = datetime(1996, 3, 4, 8)


def _db(rows):
    c = duckdb.connect()
    c.execute("""CREATE TABLE drives (drive_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP,
                 reported_distance_m DOUBLE, match_status VARCHAR)""")
    c.execute("CREATE TABLE drive_points (drive_id VARCHAR, seq INTEGER, ts TIMESTAMP, lat DOUBLE, lng DOUBLE)")
    for t in ("drive_edges", "drive_gaps"):
        c.execute(f"CREATE TABLE {t} (drive_id VARCHAR)")
    for did, minutes, miles, status, pts in rows:
        c.execute("INSERT INTO drives VALUES (?, ?, ?, ?, ?)", [did, T, T + timedelta(minutes=minutes), miles * 1609.344, status])
        for i, (lat, lng) in enumerate(pts):
            c.execute("INSERT INTO drive_points VALUES (?, ?, ?, ?, ?)", [did, i, T + timedelta(minutes=i), lat, lng])
    return c


def test_flights_and_impossible_speeds_are_set_aside_real_drives_are_not():
    c = _db([
        ("flight", 300, 1100, "failed", [(21.30, -157.85), (36.0, -120.0)]),      # ~2,500 mi between two points
        ("ghost", 100, 280, "failed", [(21.30, -157.85), (21.30, -157.85)]),      # 280 mi claimed in 100 min, no movement
        ("blip", 1, 5, "partial", [(21.30, -157.85), (21.36, -157.85)]),          # 300 mph on paper: real, matched
        ("short", 2, 4, "failed", [(21.30, -157.85), (21.35, -157.85)]),          # fast but under 10 minutes: kept
        ("trip", 150, 240, "partial", [(21.30, -157.85), (21.60, -158.10)]),      # 96 mph average, but matched: kept
    ])
    assert sorted(d for d, _ in find(c)) == ["flight", "ghost"]
    moved = set_aside(c)
    assert {d for d, _ in moved} == {"flight", "ghost"}
    assert sorted(r[0] for r in c.execute("SELECT drive_id FROM drives").fetchall()) == ["blip", "short", "trip"]
    assert c.execute("SELECT count(*) FROM not_drives").fetchone()[0] == 2
    assert c.execute("SELECT count(*) FROM drive_points WHERE drive_id IN ('flight', 'ghost')").fetchone()[0] == 0
