"""drivelog.facets: shopping-site filters over drives (invented places and dates, no real data)."""

from datetime import datetime, timedelta

import duckdb
import pytest

from drivelog.facets import Engine

# drive id, local start, miles, roads, places
DRIVES = [
    ("a", datetime(1996, 3, 4, 8, 0), 10.0, ["Quillfeather Lane"], ["town:wex", "county:north", "state:aa", "country:xx"]),   # Mon
    ("b", datetime(1996, 3, 9, 13, 0), 30.0, ["Quillfeather Lane", "Route 991"], ["town:wex", "county:north", "state:aa", "country:xx"]),  # Sat
    ("c", datetime(1997, 6, 1, 18, 30), 50.0, ["Route 991"], ["town:emb", "county:north", "state:aa", "country:xx"]),  # Sun
    ("d", datetime(1997, 6, 3, 7, 15), 0.5, ["Bramblewick Street"], ["town:lon", "state:pp", "country:yy"]),              # Tue
    ("e", datetime(1997, 6, 4, 22, 0), 4.0, ["Bramblewick Street #2"], ["town:wex", "county:north", "state:aa", "country:xx"]),  # Wed
]
AREAS = {
    "town:wex": ("locality", "Wexbury", "North County", "AA", "XX"),
    "town:emb": ("locality", "Emberton", "North County", "AA", "XX"),
    "town:lon": ("locality", "Lonmoor", None, "PP", "YY"),
    "county:north": ("county", "North County", None, "AA", "XX"),
    "state:aa": ("state", "Alphastate", None, "AA", "XX"),
    "state:pp": ("state", "Pemberland", None, "PP", "YY"),
    "country:xx": ("country", "Xland", None, None, "XX"),
    "country:yy": ("country", "Yland", None, None, "YY"),
}


@pytest.fixture()
def eng():
    c = duckdb.connect()
    c.execute("""CREATE TABLE drives (drive_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, local_date DATE,
                 local_start TIMESTAMP, reported_distance_m DOUBLE, match_status VARCHAR, matched_length_m DOUBLE)""")
    c.execute("CREATE TABLE road_drives (road_key VARCHAR, drive_id VARCHAR, length_m DOUBLE)")
    c.execute("CREATE TABLE roads (road_key VARCHAR, label VARCHAR)")
    c.execute("CREATE TABLE road_place (road_key VARCHAR, place VARCHAR)")
    c.execute("CREATE TABLE drive_places (drive_id VARCHAR, area_id VARCHAR, m DOUBLE)")
    c.execute("""CREATE TABLE areas (area_id VARCHAR, kind VARCHAR, name VARCHAR, county VARCHAR,
                 state_code VARCHAR, state_name VARCHAR, country VARCHAR)""")
    for did, ts, mi, roads, places in DRIVES:
        m = mi * 1609.344
        c.execute("INSERT INTO drives VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)",
                  [did, ts, ts + timedelta(minutes=30), ts.date(), ts, m, m])
        for r in roads:
            c.execute("INSERT INTO road_drives VALUES (?, ?, ?)", [r, did, m / len(roads)])
        for a in places:
            c.execute("INSERT INTO drive_places VALUES (?, ?, ?)", [did, a, m])
    for r in ["Quillfeather Lane", "Route 991", "Bramblewick Street", "Bramblewick Street #2"]:
        c.execute("INSERT INTO roads VALUES (?, ?)", [r, r])
    c.execute("INSERT INTO road_place VALUES ('Bramblewick Street', 'Lonmoor, PP'), ('Bramblewick Street #2', 'Wexbury, AA')")
    for k, (kind, name, county, st, ctry) in AREAS.items():
        c.execute("INSERT INTO areas VALUES (?, ?, ?, ?, ?, NULL, ?)", [k, kind, name, county, st, ctry])
    return Engine(c)


def ids(eng, f):
    return sorted(eng.drive_ids(f))


def opt(res, facet, oid):
    return next(o for o in res["facets"][facet]["options"] if o["id"] == oid)


def test_no_filters_is_everything(eng):
    r = eng.query({})
    assert r["totals"]["drives"] == 5 and r["totals"]["mi"] == pytest.approx(94.5)
    assert r["records"]["longest"]["drive_id"] == "c"


def test_or_within_a_facet_and_across_facets(eng):
    assert ids(eng, {"dow": ["5", "6"]}) == ["b", "c"]                      # Sat OR Sun
    assert ids(eng, {"dow": ["5", "6"], "year": ["1996"]}) == ["b"]         # ... AND 1996
    assert ids(eng, {"road": ["Quillfeather Lane", "Bramblewick Street"]}) == ["a", "b", "d"]
    assert ids(eng, {"road": ["Route 991"], "town": ["town:wex"]}) == ["b"]
    assert ids(eng, {"len": ["l1"]}) == ["d"] and ids(eng, {"tod": ["evening"]}) == ["e"]


def test_facet_counts_apply_the_other_facets(eng):
    r = eng.query({"year": ["1997"]})
    # Year's own counts ignore the year filter (so 1996 can still be ticked); others respect it.
    assert opt(r, "year", "1996")["count"] == 2 and opt(r, "year", "1997")["count"] == 3
    assert opt(r, "road", "Route 991")["count"] == 1
    assert opt(r, "dow", "0")["count"] == 0      # no 1997 Monday drives: shown greyed, not hidden


def test_date_range(eng):
    assert ids(eng, {"from": "1996-03-05", "to": "1997-06-02"}) == ["b", "c"]


def test_place_hierarchy_narrows_lists(eng):
    r = eng.query({"country": ["country:yy"]})
    states = {o["id"]: o["count"] for o in r["facets"]["state"]["options"]}
    towns = {o["id"]: o["count"] for o in r["facets"]["town"]["options"]}
    assert states == {"state:pp": 1} and towns == {"town:lon": 1}
    assert r["totals"]["in_place_mi"] == pytest.approx(0.5)


def test_repeated_road_names_carry_their_place(eng):
    labels = {o["id"]: o["label"] for o in eng.query({})["facets"]["road"]["options"]}
    assert labels["Bramblewick Street"] == "Bramblewick Street · Lonmoor, PP"
    assert labels["Bramblewick Street #2"] == "Bramblewick Street · Wexbury, AA"
    assert labels["Quillfeather Lane"] == "Quillfeather Lane"


def test_search_within_a_facet(eng):
    r = eng.query({"search": {"town": "emb"}})
    assert [o["id"] for o in r["facets"]["town"]["options"]] == ["town:emb"]
