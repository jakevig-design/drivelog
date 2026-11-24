"""Phase 4: aggregate matched edges into per-way and per-road statistics.

Road keys
---------
A numbered route is one road no matter how OSM fragments or names it. Rules (applied in
`normalize_ref`), in order:

1. Uppercase, turn `-`/`.` into spaces, collapse whitespace. "I-990" → "I 990", "U.S. 991" → "US 991".
2. Interstates: "INTERSTATE 990", "I 990", "IH 990" → "I 990".
3. US routes: "US 991", "US ROUTE 991", "US HWY 991", "US HIGHWAY 991" → "US 991".
4. State-qualified: "<ST> 72", "<ST> ROUTE 72", "<STATE NAME> ROUTE 72" → "<ST> 72" (ST is a
   2-letter state/province code).
5. Unqualified: "SR 72", "STATE ROUTE 72", "ROUTE 72", "RT 72", "RTE 72", "STATE HIGHWAY 72"
   → "SR 72", which is then resolved to a qualified ref ("HI 72", "US 991", …): first by
   co-occurrence on the same OSM way, then by the most frequent qualified ref with that number,
   else it stays "SR 72".
6. Suffixes BUS/BYP/ALT/TRUCK/SPUR are kept ("US 991 BUS" is a different road from "US 991").

A way with refs belongs to every ref's road (concurrencies count for each route). A way without a
ref belongs to a named road: same normalized name, grouped into connected pieces so two different
"Oak Street"s in different towns stay separate (ways with any vertices within 500 m are joined).
Ways with neither name nor ref (ramps, service roads) get no road key.
"""

from __future__ import annotations

import collections
import json
import math
import re

import pyarrow as pa

from .config import Config, load_config
from .db import connect

STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "QC", "ON",
}
STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "QUEBEC": "QC", "ONTARIO": "ON",
}
SUFFIX = r"(?:\s+(?P<suf>BUS|BUSINESS|BYP|BYPASS|ALT|ALTERNATE|TRUCK|SPUR|[NSEW]|NORTH|SOUTH|EAST|WEST|NB|SB|EB|WB))?"
NUM = r"(?P<num>\d{1,4}[A-Z]?)"

_RULES = [
    ("I", re.compile(rf"^(?:INTERSTATE|IH|I)\s*(?:HWY|HIGHWAY)?\s*{NUM}{SUFFIX}$")),
    ("US", re.compile(rf"^US\s*(?:ROUTE|RT|RTE|HWY|HIGHWAY)?\s*{NUM}{SUFFIX}$")),
    ("STATE", re.compile(rf"^(?P<st>[A-Z]{{2}})\s*(?:ROUTE|RT|RTE|HWY|HIGHWAY|SR)?\s*{NUM}{SUFFIX}$")),
    ("SR", re.compile(rf"^(?:SR|STATE\s+ROUTE|STATE\s+HIGHWAY|STATE\s+ROAD|ROUTE|RT|RTE)\s*{NUM}{SUFFIX}$")),
    ("LOCAL", re.compile(rf"^(?P<pfx>CR|CO|TR|T|A)\s*{NUM}{SUFFIX}$")),
]
DIRECTIONS = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST", "NB", "SB", "EB", "WB"}
_SUFFIX_CANON = {"BUSINESS": "BUS", "BYPASS": "BYP", "ALTERNATE": "ALT"}


def normalize_ref(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"-", " ", raw.upper().replace(".", ""))
    s = re.sub(r"\s+", " ", s).strip()
    for full, abbr in STATE_NAMES.items():
        if s.startswith(full + " "):
            s = abbr + s[len(full):]
    for kind, rx in _RULES:
        m = rx.match(s)
        if not m:
            continue
        num = m.group("num")
        suf = m.group("suf")
        tail = f" {_SUFFIX_CANON.get(suf, suf)}" if suf and suf not in DIRECTIONS else ""
        if kind == "STATE":
            st = m.group("st")
            if st == "SR":
                return f"SR {num}{tail}"
            if st not in STATES:
                continue
            return f"{st} {num}{tail}"
        if kind == "LOCAL":
            return f"{m.group('pfx')} {num}{tail}"
        return f"{kind} {num}{tail}"
    return None


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    return re.sub(r"\s+", " ", name).strip()


def _dist_m(a, b) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111_320 * math.cos(lat), (a[1] - b[1]) * 110_540)


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def assign_road_keys(ways: dict[int, dict]) -> tuple[dict[int, list[str]], dict[str, dict]]:
    """ways: way_id -> {name, refs:[raw], names:set, geometry:[[lng,lat],...]}
    Returns (way_id -> [road_key], road_key -> {label, ref, names})."""
    # Pass 1: normalize refs (from the ref field and from route-like street names).
    way_refs: dict[int, set[str]] = {}
    cooc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    qualified_by_num: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for wid, w in ways.items():
        refs = set()
        for raw in w["refs"]:
            for part in re.split(r"[;/]", raw):
                n = normalize_ref(part)
                if n:
                    refs.add(n)
        for nm in w["names"]:
            n = normalize_ref(nm)
            if n:
                refs.add(n)
        qualified = {r for r in refs if not r.startswith("SR ")}
        for r in refs:
            if r.startswith("SR "):
                for q in qualified:
                    if q.split(" ", 1)[1] == r.split(" ", 1)[1]:
                        cooc[r][q] += 1
        for q in qualified:
            qualified_by_num[q.split(" ", 1)[1]][q] += 1
        way_refs[wid] = refs

    def resolve(r: str) -> str:
        if not r.startswith("SR "):
            return r
        if cooc[r]:
            return cooc[r].most_common(1)[0][0]
        num = r.split(" ", 1)[1]
        # Prefer state routes over US/I routes for an unqualified "SR"/"Route" number.
        cands = qualified_by_num.get(num)
        if cands:
            state = [(c, n) for c, n in cands.items() if c.split(" ")[0] in STATES]
            return max(state or cands.items(), key=lambda x: x[1])[0]
        return r

    way_keys: dict[int, list[str]] = {}
    road_meta: dict[str, dict] = {}
    for wid, refs in way_refs.items():
        keys = sorted({resolve(r) for r in refs})
        way_keys[wid] = keys
        for k in keys:
            meta = road_meta.setdefault(k, {"label": k, "ref": k, "names": collections.Counter()})
            for nm in ways[wid]["names"]:
                if not normalize_ref(nm):
                    meta["names"][nm] += 1

    # Pass 2: named roads for ways without refs, split into spatially connected pieces.
    by_name: dict[str, list[int]] = collections.defaultdict(list)
    for wid, w in ways.items():
        if way_keys[wid]:
            continue
        nm = normalize_name(w["name"])
        if nm:
            by_name[nm].append(wid)
    for nm, wids in by_name.items():
        uf = _UF(len(wids))
        # Join ways whose geometries come within JOIN_M of each other, comparing all vertices, not
        # just endpoints: a way's stored geometry is only its longest *driven* part, so endpoints of
        # adjacent ways often don't meet. Grid cells of ~0.005° keep the pair checks local.
        grid = collections.defaultdict(list)
        for i, wid in enumerate(wids):
            g = ways[wid]["geometry"]
            step = max(1, len(g) // 40)
            for pt in g[::step] + g[-1:]:
                grid[(round(pt[0] / 0.005), round(pt[1] / 0.005))].append((i, pt))
        for (gx, gy), items in grid.items():
            neigh = [x for dx in (-1, 0, 1) for dy in (-1, 0, 1) for x in grid.get((gx + dx, gy + dy), [])]
            for i, pa_ in items:
                for j, pb in neigh:
                    if i < j and uf.find(i) != uf.find(j) and _dist_m(pa_, pb) <= JOIN_M:
                        uf.union(i, j)
        comps = collections.defaultdict(list)
        for i, wid in enumerate(wids):
            comps[uf.find(i)].append(wid)
        ordered = sorted(comps.values(), key=len, reverse=True)
        for n, comp in enumerate(ordered):
            key = nm if n == 0 else f"{nm} #{n + 1}"
            road_meta[key] = {"label": nm, "ref": None, "names": collections.Counter({nm: len(comp)})}
            for wid in comp:
                way_keys[wid] = [key]

    for meta in road_meta.values():
        meta["names"] = [n for n, _ in meta["names"].most_common(5)]
    return way_keys, road_meta


JOIN_M = 500

ROAD_CLASS_RANK = {
    "motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4,
    "unclassified": 5, "residential": 6, "service_other": 7,
}


def run(cfg: Config | None = None) -> dict:
    cfg = cfg or load_config()
    con = connect(cfg)

    # Per-way attributes: most common name, all refs/names seen, longest geometry.
    rows = con.execute("""
        SELECT way_id, name, ref, road_class, geometry, length_m
        FROM drive_edges e JOIN drives d USING (drive_id)
        WHERE way_id IS NOT NULL AND d.match_status IN ('ok', 'partial')
    """).fetchall()
    ways: dict[int, dict] = {}
    for wid, name, ref, rc, geom, length in rows:
        w = ways.setdefault(wid, {"name_counts": collections.Counter(), "refs": set(), "names": set(),
                                  "road_class": rc, "geometry": None, "glen": -1})
        if name:
            w["name_counts"][name] += 1
            w["names"].add(name)
        if ref:
            w["refs"].add(ref)
        if length > w["glen"]:
            w["glen"], w["geometry"] = length, geom
    for w in ways.values():
        w["name"] = w["name_counts"].most_common(1)[0][0] if w["name_counts"] else None
        w["geometry"] = json.loads(w["geometry"]) if w["geometry"] else []

    way_keys, road_meta = assign_road_keys(ways)

    con.execute("BEGIN")
    con.execute("DROP TABLE IF EXISTS way_info")
    con.execute("""CREATE TABLE way_info (way_id BIGINT PRIMARY KEY, name VARCHAR, ref VARCHAR,
                   road_class VARCHAR, geometry VARCHAR)""")
    wids = list(ways)
    con.register("_way_info", pa.table({
        "way_id": wids,
        "name": [ways[w]["name"] for w in wids],
        "ref": [";".join(sorted(ways[w]["refs"])) or None for w in wids],
        "road_class": [ways[w]["road_class"] for w in wids],
        "geometry": [json.dumps(ways[w]["geometry"], separators=(",", ":")) for w in wids],
    }))
    con.execute("INSERT INTO way_info SELECT * FROM _way_info")
    con.unregister("_way_info")
    con.execute("DROP TABLE IF EXISTS way_roads")
    con.execute("CREATE TABLE way_roads (way_id BIGINT, road_key VARCHAR)")
    wr = [(wid, k) for wid, ks in way_keys.items() for k in ks]
    if wr:
        con.register("_way_roads", pa.table({"way_id": [w for w, _ in wr], "road_key": [k for _, k in wr]}))
        con.execute("INSERT INTO way_roads SELECT * FROM _way_roads")
        con.unregister("_way_roads")
    con.execute("DROP TABLE IF EXISTS roads")
    con.execute("CREATE TABLE roads (road_key VARCHAR PRIMARY KEY, label VARCHAR, ref VARCHAR, names VARCHAR[])")
    if road_meta:
        con.executemany("INSERT INTO roads VALUES (?, ?, ?, ?)",
                        [(k, m["label"], m["ref"], m["names"]) for k, m in road_meta.items()])

    # Every traversal of a way by a matched drive.
    con.execute("DROP TABLE IF EXISTS traversals")
    # observed = the drive's GPS points put it on this way (not only a routed gap fill).
    con.execute("""
        CREATE TABLE traversals AS
        SELECT e.drive_id, e.way_id, d.local_date AS date, d.local_start, sum(e.length_m) AS length_m,
               bool_or(NOT e.inferred) AS observed,
               CASE WHEN bool_or(NOT e.inferred) THEN 'observed'
                    WHEN bool_or(e.confidence = 'high') THEN 'high' ELSE 'low' END AS confidence
        FROM drive_edges e JOIN drives d USING (drive_id)
        WHERE e.way_id IS NOT NULL AND d.match_status IN ('ok', 'partial')
        GROUP BY e.drive_id, e.way_id, d.local_date, d.local_start
    """)

    con.execute("DROP TABLE IF EXISTS road_stats")
    con.execute("""
        CREATE TABLE road_stats AS
        SELECT t.way_id, w.name, w.ref, w.road_class,
               count(DISTINCT t.drive_id) AS drive_count,
               count(DISTINCT t.drive_id) FILTER (WHERE t.observed) AS observed_drive_count,
               count(DISTINCT t.date) AS distinct_days,
               sum(t.length_m) / 1000 AS total_km,
               min(t.date) AS first_date, max(t.date) AS last_date
        FROM traversals t JOIN way_info w USING (way_id)
        GROUP BY ALL
    """)

    con.execute("DROP TABLE IF EXISTS named_road_stats")
    con.execute("""
        CREATE TABLE named_road_stats AS
        SELECT r.road_key, r.label, r.ref, r.names,
               arg_min(w.road_class, CASE w.road_class
                   WHEN 'motorway' THEN 0 WHEN 'trunk' THEN 1 WHEN 'primary' THEN 2
                   WHEN 'secondary' THEN 3 WHEN 'tertiary' THEN 4 WHEN 'unclassified' THEN 5
                   WHEN 'residential' THEN 6 ELSE 7 END) AS road_class,
               count(DISTINCT t.drive_id) AS drive_count,
               count(DISTINCT t.drive_id) FILTER (WHERE t.observed) AS observed_drive_count,
               count(DISTINCT t.date) AS distinct_days,
               sum(t.length_m) / 1000 AS total_km,
               min(t.date) AS first_date, max(t.date) AS last_date,
               count(DISTINCT t.way_id) AS n_ways
        FROM traversals t
        JOIN way_roads wr USING (way_id)
        JOIN roads r USING (road_key)
        JOIN way_info w USING (way_id)
        GROUP BY r.road_key, r.label, r.ref, r.names
    """)

    # One row per (road, drive): the basis for "every time I drove this road" and similar-drive lookups.
    con.execute("DROP TABLE IF EXISTS road_drives")
    con.execute("""
        CREATE TABLE road_drives AS
        SELECT wr.road_key, t.drive_id, t.date, t.local_start, sum(t.length_m) AS length_m,
               bool_or(t.observed) AS observed,
               CASE WHEN bool_or(t.observed) THEN 'observed'
                    WHEN bool_or(t.confidence = 'high') THEN 'high' ELSE 'low' END AS confidence
        FROM traversals t JOIN way_roads wr USING (way_id)
        GROUP BY wr.road_key, t.drive_id, t.date, t.local_start
    """)

    # First drive on each road and each way ("new roads" on a drive). Any traversal counts, observed
    # or inferred; the drive view shows which.
    con.execute("DROP TABLE IF EXISTS road_first")
    con.execute("""
        CREATE TABLE road_first AS
        SELECT road_key, arg_min(drive_id, local_start) AS drive_id, min(local_start) AS local_start
        FROM road_drives GROUP BY road_key
    """)
    con.execute("DROP TABLE IF EXISTS way_first")
    con.execute("""
        CREATE TABLE way_first AS
        SELECT way_id, arg_min(drive_id, local_start) AS drive_id FROM traversals GROUP BY way_id
    """)

    con.execute("DROP TABLE IF EXISTS road_dates")
    con.execute("""
        CREATE TABLE road_dates AS
        SELECT wr.road_key, t.date, list(DISTINCT t.drive_id ORDER BY t.drive_id) AS drive_ids
        FROM traversals t JOIN way_roads wr USING (way_id)
        GROUP BY ALL
    """)
    con.execute("COMMIT")

    export_geojson(con, cfg.db_path.parent / "geo/roads.geojson")
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('way_areas', 'areas')").fetchone()[0] == 2:
        from .facets import build_places_from_roads, ensure_path_m

        ensure_path_m(con)
        build_places_from_roads(con)  # traversals changed: refresh the dashboard's per-drive places
    stats = {
        "ways": con.execute("SELECT count(*) FROM road_stats").fetchone()[0],
        "roads": con.execute("SELECT count(*) FROM named_road_stats").fetchone()[0],
        "road_dates": con.execute("SELECT count(*) FROM road_dates").fetchone()[0],
    }
    con.close()
    return stats


def export_geojson(con, out) -> None:
    rows = con.execute("""
        SELECT s.way_id, s.name, s.ref, s.road_class, s.drive_count, s.distinct_days,
               round(s.total_km, 2), s.first_date::VARCHAR, s.last_date::VARCHAR, w.geometry,
               (SELECT list(road_key) FROM way_roads wr WHERE wr.way_id = s.way_id)
        FROM road_stats s JOIN way_info w USING (way_id)
    """).fetchall()
    feats = []
    for wid, name, ref, rc, dc, dd, km, fd, ld, geom, keys in rows:
        g = json.loads(geom)
        if len(g) < 2:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": g},
            "properties": {"way_id": wid, "name": name, "ref": ref, "road_class": rc, "drive_count": dc,
                           "distinct_days": dd, "total_km": km, "first_date": fd, "last_date": ld,
                           "road_keys": keys or []},
        })
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
