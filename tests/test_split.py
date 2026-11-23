"""drivelog.split: stitched drives are cut at 10+ minute stops, ordinary drives are left alone."""

from datetime import datetime, timedelta

from drivelog.split import find_stops, keep, split_points

T0 = datetime(1996, 3, 4, 8, 0)
LAT, LNG = 21.40, -157.80  # Oahu, like the demo data


def pt(minute: float, north_m: float = 0.0):
    return (T0 + timedelta(minutes=minute), LAT + north_m / 111_320, LNG)


def route_at(speed_ms: float):
    """Fake Valhalla: route time = straight distance / speed."""
    from drivelog.geo import haversine_m

    return lambda a, b: haversine_m(a[1], a[2], b[1], b[2]) / speed_ms


def drive(start_min: float, start_m: float, km: float, minutes: float, step: float = 2):
    """Points every `step` minutes moving north at a steady speed."""
    n = int(minutes / step)
    return [pt(start_min + i * step, start_m + km * 1000 * i / n) for i in range(n + 1)]


def test_observed_stop_splits_into_two_drives():
    a = drive(0, 0, 8, 12)                       # 8 km in 12 min
    parked = [pt(12 + m, 8000 + (m % 3) * 20) for m in (5, 15, 30, 44)]  # 44 min within 100 m
    b = drive(57, 8000, 6, 10)[1:]
    pts = a + parked + b
    stops = find_stops(pts, route_at(15), stationary_m=100, pace=1.0)
    assert [s.kind for s in stops] == ["observed"]
    assert stops[0].seconds >= 40 * 60
    pieces = keep(split_points(pts, stops), 2, 500)
    assert len(pieces) == 2
    assert pieces[0].points[-1] == a[-1]          # ends on arrival at the stop
    assert pieces[1].points[0] == parked[-1]      # restarts from the last time seen parked
    assert not pieces[1].est_start


def test_hidden_stop_in_a_sparse_gap():
    a = drive(0, 0, 8, 12)
    b = drive(12 + 240, 30_000, 5, 8)             # 4 h later and 22 km further: a stop hidden in the gap
    pts = a + b
    stops = find_stops(pts, route_at(15), stationary_m=100, pace=1.0)
    assert [s.kind for s in stops] == ["hidden"]
    pieces = keep(split_points(pts, stops), 2, 500)
    assert len(pieces) == 2
    second = pieces[1]
    assert second.est_start and second.last and not second.first
    # It leaves from where the car was last seen, timed to arrive at the next point at the usual pace.
    assert second.points[0][1:] == a[-1][1:]
    drive_s = (second.points[1][0] - second.points[0][0]).total_seconds()
    assert abs(drive_s - 22_000 / 15) < 2


def test_ordinary_drive_is_not_split():
    # Steady driving, a 4-minute wait at a light, and a sparse 25-minute gap that driving explains.
    a = drive(0, 0, 6, 10)
    light = [pt(10 + m, 6000 + 10 * m) for m in (1, 2, 3, 4)]
    b = drive(14, 6040, 4, 6)[1:]
    far = [pt(45, 6040 + 4000 + 25_000)]          # 25 km in 25 min, route at 15 m/s needs ~28 min
    pts = a + light + b + far
    assert find_stops(pts, route_at(15), stationary_m=100, pace=1.0) == []
    pieces = split_points(pts, [])
    assert len(pieces) == 1 and pieces[0].points == pts and pieces[0].first and pieces[0].last


def test_parked_lead_in_and_short_pieces():
    # A 30-minute wait at the very start is trimmed off: one drive, starting the last time seen parked.
    pts = [pt(0), pt(30, 20)] + drive(31, 20, 8, 12)[1:]
    stops = find_stops(pts, route_at(15), stationary_m=100, pace=1.0)
    pieces = keep(split_points(pts, stops), 2, 500)
    assert len(pieces) == 1 and pieces[0].points[0] == pts[1] and not pieces[0].first
    # Minute rounding: 10 minutes 30 s of excess is under the 10-minute rule plus a minute of rounding.
    pts = drive(0, 0, 8, 12) + [pt(12 + 10.5, 8050)] + drive(23, 8050, 6, 10)[1:]
    assert find_stops(pts, route_at(15), stationary_m=100, pace=1.0) == []
    # A stop followed by a 200 m shuffle: the short piece is dropped, one drive is left.
    a = drive(0, 0, 8, 12)
    pts = a + [pt(40, 8010), pt(42, 8210)]
    stops = find_stops(pts, route_at(15), stationary_m=100, pace=1.0)
    assert len(stops) == 1
    assert len(keep(split_points(pts, stops), 2, 500)) == 1
