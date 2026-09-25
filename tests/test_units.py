from drivelog.aggregate import assign_road_keys, normalize_ref
from drivelog.geo import parse_coord
from drivelog.match import Edge, decode_polyline6, is_ref, merge_edges


def test_parse_coord_formats():
    assert parse_coord("geo:21.3069,-157.8583") == (21.3069, -157.8583)
    assert parse_coord("21.3069°, -157.8583°") == (21.3069, -157.8583)
    assert parse_coord("Oneawa Street") is None


def test_polyline6_roundtrip_known():
    # Valhalla docs example: [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)] at precision 6
    assert decode_polyline6("_izlhA~rlgdF_{geC~ywl@_kwzCn`{nI") == [
        (38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_normalize_ref_variants():
    for raw in ["HI 72", "HI-72", "Hawaii Route 72"]:
        assert normalize_ref(raw) == "HI 72"
    for raw in ["Route 72", "SR 72", "State Route 72", "RT 72"]:
        assert normalize_ref(raw) == "SR 72"
    assert normalize_ref("I-990") == normalize_ref("Interstate 990") == "I 990"
    assert normalize_ref("U.S. 991") == normalize_ref("US Route 991") == "US 991"
    assert normalize_ref("US 991 Business") == "US 991 BUS"
    assert normalize_ref("Kapiolani Boulevard") is None


def test_unqualified_route_resolves_to_state_ref():
    ways = {
        1: {"name": "Route 72", "names": {"Route 72"}, "refs": {"HI 72"}, "geometry": [[0, 0], [0, 0.01]]},
        2: {"name": "Route 72", "names": {"Route 72"}, "refs": set(), "geometry": [[0, 0.02], [0, 0.03]]},
        3: {"name": "Oneawa Street", "names": {"Oneawa Street"}, "refs": set(), "geometry": [[1, 1], [1, 1.001]]},
        4: {"name": "Oneawa Street", "names": {"Oneawa Street"}, "refs": set(), "geometry": [[2, 2], [2, 2.001]]},
    }
    keys, meta = assign_road_keys(ways)
    assert keys[1] == ["HI 72"] and keys[2] == ["HI 72"]
    # Two far-apart same-named streets stay separate roads.
    assert keys[3] != keys[4]


def test_is_ref():
    assert is_ref("HI 72") and is_ref("I 990") and is_ref("US 991")
    assert not is_ref("Kapiolani Boulevard")


def test_merge_edges_same_way():
    a = Edge(1, "A St", None, "residential", 100, [[0, 0], [0, 1]])
    b = Edge(1, "A St", None, "residential", 50, [[0, 1], [0, 2]])
    c = Edge(2, "B St", None, "residential", 10, [[0, 2], [0, 3]])
    m = merge_edges([a, b, c])
    assert [e.way_id for e in m] == [1, 2]
    assert m[0].length_m == 150 and m[0].geometry == [[0, 0], [0, 1], [0, 2]]


def test_drop_outliers_removes_bad_endpoint_only():
    from datetime import datetime, timedelta

    from drivelog.extract import drop_outliers

    t = datetime(1997, 1, 1)
    far_start = (t, 12.40, 10.00)  # ~260 km from the others, same minute as the next point (synthetic)
    good = [(t + timedelta(minutes=i), 10.00 + 0.005 * i, 10.00) for i in range(1, 6)]
    assert drop_outliers([far_start, *good]) == good
    assert drop_outliers(good) == good


def test_direction_suffixes_dropped():
    assert normalize_ref("I 990 East") == normalize_ref("I-990 W") == normalize_ref("I 990 EB") == "I 990"
    assert normalize_ref("US 991 West") == "US 991"


def _pt(minute, lat, lng):
    from datetime import datetime, timedelta, timezone

    return (datetime(1997, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute), lat, lng)


def test_assemble_records_gaps_with_trace_times():
    from drivelog.match import Edge, Gap, PieceResult, assemble

    # Two observed pieces ~11 km apart (synthetic coordinates), plus the drive's own start/end.
    p1 = PieceResult(edges=[Edge(1, "A", None, "primary", 1000, [[10.0, 10.0], [10.0, 10.009]])],
                     first=_pt(0, 10.0, 10.0), last=_pt(2, 10.009, 10.0))
    p2 = PieceResult(edges=[Edge(2, "B", None, "primary", 1000, [[10.0, 10.1], [10.0, 10.109]])],
                     first=_pt(10, 10.1, 10.0), last=_pt(12, 10.109, 10.0))
    items = assemble([p1, p2], _pt(0, 10.0, 10.0), _pt(12, 10.109, 10.0))
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["Edge", "Gap", "Edge"]
    gap = items[1]
    assert isinstance(gap, Gap) and gap.a_ts == p1.last[0] and gap.b_ts == p2.first[0]
    assert 9000 < gap.straight_m < 11000


def test_merge_edges_keeps_inferred_separate():
    from drivelog.match import Edge, merge_edges

    a = Edge(1, "A", None, "primary", 100, [[0, 0], [0, 1]])
    b = Edge(1, "A", None, "primary", 100, [[0, 1], [0, 2]], inferred=True, confidence="high")
    assert len(merge_edges([a, b])) == 2


def test_segments_drop_slivers_and_mark_confidence():
    from drivelog.queries import _segments

    def e(way, name, meters, inferred=False, conf="observed"):
        return (0, way, name, None, "primary", meters, "[]", [name], inferred, conf)

    segs = _segments([e(1, "Main", 1609), e(2, "Tiny", 20), e(3, "Main", 1609, True, "low"), e(4, "Oak", 3218, True, "high")],
                     new_keys={"Oak"})
    assert [s["label"] for s in segs] == ["Main", "Oak"]  # sliver dropped, Main re-merged
    assert round(segs[0]["mi"], 1) == 2.0 and segs[0]["confidence"] == "observed"
    assert segs[1]["confidence"] == "high" and segs[1]["new"]


def test_finalize_status_rules():
    from drivelog.fill import finalize

    ok, _, d = finalize({"point_share": 1.0}, 10_000, 0, 0, 10_000)
    assert ok == "ok" and d["length_ratio"] == 1.0
    mostly_routed, _, _ = finalize({"point_share": 1.0}, 2_000, 8_000, 0, 10_000)
    assert mostly_routed == "partial"
    assert finalize({}, 0, 0, 0, 10_000)[0] == "failed"


# ---------------------------------------------------------------- v2 phase 1: parked lead-in / tail


def test_trim_stationary_lead_in_and_tail():
    from drivelog.extract import moving_window, trim_stationary

    # Synthetic: 3 min parked (jitter < 100 m), 4 points moving ~1.1 km apart, 2 min parked at the end.
    parked = [_pt(i, 10.0 + 0.0001 * i, 10.0) for i in range(4)]            # minutes 0..3
    moving = [_pt(4 + i, 10.0 + 0.01 * (i + 1), 10.0) for i in range(4)]
    end = moving[-1]
    tail = [_pt(8 + i, end[1] + 0.0001 * (i + 1), 10.0) for i in range(2)]  # minutes 8..9
    pts = parked + moving + tail
    t = trim_stationary(pts, 100)
    assert t[0] == parked[-1]          # last parked point kept: same place, time the car set off
    assert t[-1] == moving[-1]         # first point of the parked tail = arrival
    assert moving_window(pts, 100) == (parked[-1][0], moving[-1][0])


def test_trim_stationary_never_moves_is_unchanged():
    from drivelog.extract import trim_stationary

    pts = [_pt(i, 10.0 + 0.0001 * i, 10.0) for i in range(5)]
    assert trim_stationary(pts, 100) == pts
    assert trim_stationary(pts[:2], 100) == pts[:2]


def test_elapsed_clamped_to_moving_window():
    from drivelog.fill import elapsed_s
    from drivelog.match import Gap

    g = Gap([10.0, 10.0], [10.0, 10.05], _pt(0, 0, 0)[0], _pt(20, 0, 0)[0])
    assert elapsed_s(g) == 1200
    # Parked for the first 8 minutes: only 12 minutes of driving explain the gap.
    assert elapsed_s(g, (_pt(8, 0, 0)[0], _pt(30, 0, 0)[0])) == 720
    assert elapsed_s(g, (_pt(8, 0, 0)[0], _pt(15, 0, 0)[0])) == 420
    assert elapsed_s(Gap([0, 0], [0, 1], None, None), (_pt(0, 0, 0)[0], _pt(1, 0, 0)[0])) is None


def test_rate_uses_pace_rounding_and_speed_cap():
    from drivelog.fill import rate

    # 8 km route Valhalla times at 13 min, driven in 5 min on familiar roads.
    assert rate(6400, 8000, 780, 300, 1.0, pace=1.0) == "low"    # 0.38 of Valhalla's time, unscaled
    assert rate(6400, 8000, 780, 300, 1.0, pace=0.77) == "high"  # 0.50 of the driver's usual time
    # Rounding: 4 min vs an expected 10 is outside 0.5–2.5, but 5 min (4 + 1) isn't.
    assert rate(6400, 8000, 600, 240, 1.0) == "high"
    assert rate(6400, 8000, 600, 150, 1.0) == "low"
    # 140 km in 8 minutes is broken timestamps, not a fast drive.
    assert rate(140_000, 160_000, 6000, 480, 1.0, pace=0.77) == "low"
    # Unfamiliar route: only the time test counts; untimed is low; short gaps are always high.
    assert rate(6400, 8000, 600, 580, 0.1) == "high"
    assert rate(6400, 8000, 600, None, 0.1) == "low"
    assert rate(900, 1200, 90, 9999, 0.0) == "high"


def test_pace_survives_a_rematch():
    import duckdb

    from drivelog.db import SCHEMA
    from drivelog.fill import load_pace, save_pace

    con = duckdb.connect()
    con.execute(SCHEMA)
    assert load_pace(con) == 1.0
    con.executemany("INSERT INTO drive_gaps (drive_id, seq, straight_m, filled, route_s, elapsed_s) VALUES (?, 0, 5000, true, 600, ?)",
                    [(f"d{i}", 400 + i % 100) for i in range(300)])
    pace = load_pace(con)
    assert 0.73 < pace < 0.76
    save_pace(con)
    con.execute("DELETE FROM drive_gaps")  # what a rematch does before filling
    assert load_pace(con) == pace


def test_ios_and_android_demo_exports_give_the_same_drives():
    """The two synthetic demo exports describe the same drives in each format's own layout."""
    from pathlib import Path

    from drivelog.config import Config
    from drivelog.extract import ADAPTERS, build_drives, detect_format

    root = Path(__file__).resolve().parents[1] / "demo"
    cfg = Config({"time": {"fallback_tz": "Pacific/Honolulu"}})
    out = {}
    for name in ("timeline_ios.json", "timeline_android.json"):
        fmt = detect_format(root / name)
        drives, _ = build_drives(*ADAPTERS[fmt](root / name).read(), cfg)
        out[fmt] = drives
    ios, android = out["ios"], out["android"]
    assert len(ios) == len(android) > 50
    assert [d.drive_id for d in ios] == [d.drive_id for d in android]
    assert all(d.utc_offset_min == -600 for d in android)
    # iOS path times are minute offsets, Android's are exact: the point counts match, positions agree.
    assert [len(d.points) for d in ios] == [len(d.points) for d in android]
