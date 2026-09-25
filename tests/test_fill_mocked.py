"""Gap filling against a mocked Valhalla (no routing server needed, e.g. in CI)."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from drivelog.config import Config
from drivelog.coverage import Coverage
from drivelog.fill import fill_gap
from drivelog.match import Gap, Matcher


def _poly6(coords):
    """Encode [(lat, lng)] as a precision-6 polyline (the inverse of match.decode_polyline6)."""
    out, plat, plng = [], 0, 0
    for lat, lng in coords:
        for v, prev in ((round(lat * 1e6), plat), (round(lng * 1e6), plng)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plng = round(lat * 1e6), round(lng * 1e6)
    return "".join(out)


# Two candidate routes between the same points (synthetic coordinates): a fast one on way 1 and a slow
# detour on way 2. Way 1 is also a road "you" drive regularly.
FAST = [(10.0, 10.0), (10.05, 10.0)]
SLOW = [(10.0, 10.0), (10.02, 10.03), (10.05, 10.0)]


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if request.url.path == "/route":
        trip = lambda shape, secs, km: {"summary": {"time": secs, "length": km}, "legs": [{"shape": _poly6(shape)}]}  # noqa: E731
        return httpx.Response(200, json={"trip": trip(FAST, 600, 5.6), "alternates": [{"trip": trip(SLOW, 900, 8.0)}]})
    if request.url.path == "/trace_attributes":  # edge_walk of a route shape
        way = 1 if body["encoded_polyline"] == _poly6(FAST) else 2
        shape = FAST if way == 1 else SLOW
        return httpx.Response(200, json={"shape": body["encoded_polyline"], "edges": [
            {"way_id": way, "names": [f"Way {way}"], "road_class": "primary", "length": 5.6 if way == 1 else 8.0,
             "begin_shape_index": 0, "end_shape_index": len(shape) - 1}]})
    return httpx.Response(404)


def _matcher():
    m = Matcher(Config({"valhalla": {"url": "http://valhalla.test"}}), Coverage([]))
    m.client = httpx.Client(transport=httpx.MockTransport(handler))
    return m


def _gap(minutes):
    t0 = datetime(2012, 5, 1, 8, 0, tzinfo=timezone.utc)
    return Gap([10.0, 10.0], [10.0, 10.05], t0, t0 + timedelta(minutes=minutes))


def test_fill_picks_the_route_that_fits_the_time():
    f = fill_gap(_matcher(), habit=set(), gap=_gap(10))
    assert f.candidates == 2 and [e.way_id for e in f.edges] == [1]
    assert f.confidence == "high" and all(e.inferred for e in f.edges)
    slow = fill_gap(_matcher(), habit=set(), gap=_gap(15))
    assert [e.way_id for e in slow.edges] == [2]  # 15 min fits the detour better


def test_fill_parked_start_is_timed_from_the_moving_window():
    gap = _gap(40)  # 40 minutes for a 10-minute route: doesn't fit...
    t0 = gap.a_ts
    assert fill_gap(_matcher(), habit=set(), gap=gap).confidence == "low"
    # ...unless the car sat in the driveway for the first 29 of them.
    window = (t0 + timedelta(minutes=29), t0 + timedelta(minutes=60))
    assert fill_gap(_matcher(), habit=set(), gap=gap, window=window).confidence == "high"
