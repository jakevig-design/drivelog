"""Area lookup and way-to-area assignment on synthetic boundaries (invented places, not real data)."""

import json
from datetime import date, datetime

import duckdb
import pytest

from drivelog import areas


def _poly(x0, y0, x1, y1):
    return {"type": "MultiPolygon", "coordinates": [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]]}


def _admin(osm_id, name, level, geom, **tags):
    props = {"@type": "relation", "@id": osm_id, "boundary": "administrative", "admin_level": str(level),
             "name": name, **tags}
    return json.dumps({"type": "Feature", "properties": props, "geometry": geom})


@pytest.fixture()
def con(tmp_path):
    admin = tmp_path / "admin.geojsonseq"
    admin.write_text("\n".join([
        _admin(1, "Alphastate", 4, _poly(0, 0, 10, 10), **{"ISO3166-2": "US-AA"}),
        _admin(2, "Betastate", 4, _poly(10, 0, 20, 10), **{"ISO3166-2": "US-BB"}),
        _admin(3, "North County", 6, _poly(0, 5, 10, 10)),
        _admin(4, "Wexbury", 8, _poly(1, 6, 3, 8)),
        _admin(5, "Wexbury", 8, _poly(11, 1, 13, 3)),
        _admin(6, "Emberton", 8, _poly(4, 6, 6, 8)),
        _admin(7, "Some Ward", 10, _poly(4, 6, 5, 7)),  # level 10 in the US: not loaded
        json.dumps({"type": "Feature", "geometry": _poly(6.5, 6, 8, 8),  # census-designated place, no admin_level
                    "properties": {"@type": "relation", "@id": 8, "boundary": "census", "name": "Lowmoor"}}),
        json.dumps({"type": "Feature", "geometry": _poly(4, 6, 6, 8),  # the same town as Emberton, again as a CDP
                    "properties": {"@type": "relation", "@id": 9, "boundary": "census", "name": "Emberton"}}),
    ]) + "\n")
    zcta = tmp_path / "zcta.geojson"
    zcta.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"ZCTA5CE20": "99901"}, "geometry": _poly(0, 5, 5, 10)},
        {"type": "Feature", "properties": {"ZCTA5CE20": "99902"}, "geometry": _poly(5, 5, 10, 10)},
    ]}))
    c = duckdb.connect()
    ways = {
        1: [[2, 6.5], [2, 7.5]],   # inside Wexbury (AA), zip 99901
        2: [[4, 9], [6, 9]],       # crosses the 99901 / 99902 line at x = 5
        3: [[8, 2], [12, 2]],      # crosses the Alphastate / Betastate line at x = 10
    }
    c.execute("CREATE TABLE way_info (way_id BIGINT, name VARCHAR, ref VARCHAR, road_class VARCHAR, geometry VARCHAR)")
    for wid, g in ways.items():
        c.execute("INSERT INTO way_info VALUES (?, ?, NULL, 'primary', ?)", [wid, f"Road {wid}", json.dumps(g)])
    c.execute("""CREATE TABLE traversals (drive_id VARCHAR, way_id BIGINT, date DATE, local_start TIMESTAMP,
                 length_m DOUBLE, observed BOOLEAN, confidence VARCHAR)""")
    c.executemany("INSERT INTO traversals VALUES (?, ?, ?, ?, ?, true, 'observed')", [
        ("d1", 1, date(2012, 1, 1), datetime(2012, 1, 1, 9), 1000.0),
        ("d1", 2, date(2012, 1, 1), datetime(2012, 1, 1, 9), 2000.0),
        ("d2", 3, date(2012, 2, 1), datetime(2012, 2, 1, 17), 4000.0),
    ])
    c.execute("CREATE TABLE way_roads (way_id BIGINT, road_key VARCHAR)")
    c.execute("INSERT INTO way_roads VALUES (1, 'Road 1'), (2, 'Road 2'), (3, 'Road 3')")
    c.execute("CREATE TABLE roads (road_key VARCHAR, label VARCHAR, ref VARCHAR, names VARCHAR[])")
    c.execute("INSERT INTO roads VALUES ('Road 1', 'Road 1', NULL, []), ('Road 2', 'Road 2', NULL, []), ('Road 3', 'Road 3', NULL, [])")
    areas.build(admin_path=admin, zcta_path=zcta, con=c, progress=False)
    return c


def _frac(con, way_id, area_id):
    r = con.execute("SELECT frac FROM way_areas WHERE way_id = ? AND area_id = ?", [way_id, area_id]).fetchone()
    return r[0] if r else 0.0


def test_zip_lookup(con):
    a, cands = areas.resolve_area(con, "99901")
    assert a and a["kind"] == "zip" and a["state_code"] == "AA" and not cands
    s = areas.area_summary(con, a["area_id"])
    # Way 1 fully (1000 m) + half of way 2 (1000 of 2000 m).
    assert s["mi"] == pytest.approx(2000 / 1609.344, rel=1e-3)
    assert s["drive_count"] == 1 and s["distinct_days"] == 1 and "ZCTA" in s["note"]


def test_municipality_lookup(con):
    a, _ = areas.resolve_area(con, "Emberton")
    assert a["kind"] == "locality" and a["label"] == "Emberton (North County, AA)"
    assert areas.resolve_area(con, "Some Ward") == (None, [])  # level 10 isn't a US locality
    cdp, _ = areas.resolve_area(con, "Lowmoor")
    assert cdp["kind"] == "locality" and cdp["state_code"] == "AA"


def test_ambiguous_name_lists_candidates_and_qualifier_resolves(con):
    a, cands = areas.resolve_area(con, "Wexbury")
    assert a is None and sorted(c["state_code"] for c in cands) == ["AA", "BB"]
    a, _ = areas.resolve_area(con, "Wexbury, BB")
    assert a and a["state_code"] == "BB"
    a, _ = areas.resolve_area(con, "Wexbury, Alphastate")
    assert a and a["state_code"] == "AA"


def test_way_crossing_boundary_counts_toward_both_by_length(con):
    aa, bb = areas.resolve_area(con, "Alphastate")[0], areas.resolve_area(con, "Betastate")[0]
    assert _frac(con, 3, aa["area_id"]) == pytest.approx(0.5)
    assert _frac(con, 3, bb["area_id"]) == pytest.approx(0.5)
    assert _frac(con, 2, "zip:99901") == pytest.approx(0.5) and _frac(con, 2, "zip:99902") == pytest.approx(0.5)
    # The country is built from its states, and holds all of way 3.
    us = areas.resolve_area(con, "United States")[0]
    assert us["kind"] == "country" and _frac(con, 3, us["area_id"]) == pytest.approx(1.0)
    driven = {r["name"]: r["mi"] for r in areas.areas_driven(con)}
    assert driven["Betastate"] == pytest.approx(2000 / 1609.344, rel=1e-3)
