"""Areas: zip codes (Census ZCTAs), localities, counties, states/provinces and countries. Fully local.

Build (`drivelog areas build`):
  1. Zip codes from the Census 2020 ZCTA cartographic boundary file (1:500k), downloaded once into
     data/boundaries/. ZCTAs approximate USPS zip codes; they're not the same thing.
  2. Administrative boundaries from the OSM extracts already downloaded for routing:
     `osmium tags-filter r/boundary=administrative` on data/valhalla/merged.osm.pbf, then
     `osmium export` to polygons. OSM admin levels differ by country; ADMIN_KINDS maps them to a common
     kind (locality, county, state, country). Census-designated places (`boundary=census`, e.g. the
     unincorporated towns that are most of Hawaii) are loaded as localities too.
  3. Each area's state and country come from the ISO3166 tags of the state polygon containing it.
     Countries whose own boundary is cut off by the extract (the US in a regional extract) are built
     as the union of their states.
  4. way_areas: for every driven OSM way, the share of its length inside each area. A way crossing a
     boundary counts toward both sides by length.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pyarrow as pa

from .config import ROOT, Config, load_config
from .db import connect
from .queries import M_PER_MI, _rows

BOUNDARIES = ROOT / "data/boundaries"
ZCTA_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip"
ZCTA_ZIP = BOUNDARIES / "cb_2020_us_zcta520_500k.zip"
ZIP_NOTE = "Zip codes are Census ZCTAs (2020), which approximate USPS zip code areas."

# admin_level -> kind, per ISO country code. Levels not listed (neighbourhoods, wards, regions between
# state and county) are not loaded. "*" is the default for countries without an entry.
ADMIN_KINDS: dict[str, dict[int, str]] = {
    "US": {2: "country", 4: "state", 6: "county", 7: "locality", 8: "locality"},  # 7: NY/NJ towns
    "CA": {2: "country", 4: "state", 6: "county", 8: "locality"},                 # 5: QC regions, skipped
    "GB": {2: "country", 4: "state", 6: "county", 8: "locality", 10: "locality"},  # 10: parishes
    "DE": {2: "country", 4: "state", 6: "county", 8: "locality"},                 # 5: Regierungsbezirke
    "FR": {2: "country", 4: "state", 6: "county", 8: "locality"},                 # 6: départements
    "*": {2: "country", 4: "state", 6: "county", 8: "locality"},
}
KIND_ORDER = {"zip": 0, "locality": 1, "county": 2, "state": 3, "country": 4}
# Names for countries built from their states (their own boundary isn't in a regional extract).
COUNTRY_NAMES = {"US": "United States", "CA": "Canada", "MX": "Mexico"}


def kind_for(country: str | None, level: int | None) -> str | None:
    if level is None:
        return None
    return ADMIN_KINDS.get(country or "*", ADMIN_KINDS["*"]).get(level)


def _spatial(con) -> None:
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")


# ---------------------------------------------------------------- build


def fetch_zcta() -> Path:
    BOUNDARIES.mkdir(parents=True, exist_ok=True)
    if not ZCTA_ZIP.exists():
        tmp = ZCTA_ZIP.with_suffix(".part")
        subprocess.run(["curl", "-fL", "--retry", "5", "-o", str(tmp), ZCTA_URL], check=True)
        tmp.rename(ZCTA_ZIP)
    return ZCTA_ZIP


def extract_admin(cfg: Config | None = None) -> Path:
    cfg = cfg or load_config()
    pbf = cfg.valhalla_dir / "merged.osm.pbf"
    work = cfg.db_path.parent / "boundaries"  # per database: the demo keeps its own
    work.mkdir(parents=True, exist_ok=True)
    filt, out = work / "admin.osm.pbf", work / "admin.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(pbf), "r/boundary=administrative,census", "-o", str(filt),
                    "--overwrite"], check=True)
    subprocess.run(["osmium", "export", str(filt), "-f", "geojsonseq", "--geometry-types=polygon",
                    "-a", "type,id", "-o", str(out), "--overwrite"], check=True)
    return out


def read_admin(path: Path) -> list[dict]:
    """Admin polygons with the tags we use. Geometry stays a GeoJSON string for DuckDB to parse."""
    out = []
    with path.open() as f:
        for line in f:
            feat = json.loads(line.lstrip("\x1e"))
            p = feat["properties"]
            if p.get("boundary") not in ("administrative", "census") or not p.get("name"):
                continue
            try:
                # A census-designated place has no admin_level; it sits where a town would (level 8).
                level = 8 if p["boundary"] == "census" else int(p.get("admin_level", ""))
            except ValueError:
                continue
            out.append({
                "osm_id": f"{p.get('@type', 'x')[0]}{p.get('@id')}",
                "name": p.get("name:en") if p.get("name:en") and level <= 4 else p["name"],
                "level": level,
                "iso1": p.get("ISO3166-1") or p.get("ISO3166-1:alpha2"),
                "iso2": p.get("ISO3166-2"),
                "census": p["boundary"] == "census",
                "geojson": json.dumps(feat["geometry"], separators=(",", ":")),
            })
    return out


def build(cfg: Config | None = None, admin_path: Path | None = None, zcta_path: Path | None = None,
          con=None, progress: bool = True) -> dict:
    """(Re)build the areas and way_areas tables. Paths are overridable for tests."""
    cfg = cfg or load_config()
    own = con is None
    con = con or connect(cfg)
    _spatial(con)
    log = (lambda *a: print(*a, flush=True)) if progress else (lambda *a: None)

    if admin_path is None:
        log("  extracting admin boundaries (osmium)")
        admin_path = extract_admin(cfg)
    admin = read_admin(admin_path)
    log(f"  {len(admin):,} admin polygons")

    con.execute("DROP TABLE IF EXISTS _admin")
    con.execute("""CREATE TEMP TABLE _admin (osm_id VARCHAR, name VARCHAR, level INTEGER, iso1 VARCHAR,
                   iso2 VARCHAR, census BOOLEAN, geom GEOMETRY)""")
    if admin:
        con.register("_a", pa.table({k: [a[k] for a in admin] for k in admin[0]}))
        con.execute("""INSERT INTO _admin SELECT osm_id, name, level, iso1, iso2, census,
                       ST_MakeValid(ST_GeomFromGeoJSON(geojson)) FROM _a""")
        con.unregister("_a")

    # State (level 4 with ISO3166-2) and country for every admin polygon, from a point inside it.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _states AS
        SELECT osm_id, name, iso2, split_part(iso2, '-', 1) AS country, split_part(iso2, '-', 2) AS code, geom
        FROM _admin WHERE level = 4 AND iso2 IS NOT NULL""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _placed AS
        SELECT a.*, s.code AS state_code, s.name AS state_name,
               coalesce(a.iso1, split_part(a.iso2, '-', 1), s.country) AS country
        FROM (SELECT *, ST_PointOnSurface(geom) AS pt FROM _admin) a
        LEFT JOIN _states s ON a.level > 4 AND ST_Contains(s.geom, a.pt)""")
    # A polygon can sit in two overlapping state polygons (bad data); keep one.
    con.execute("""CREATE OR REPLACE TEMP TABLE _placed AS
                   SELECT * FROM _placed QUALIFY row_number() OVER (PARTITION BY osm_id ORDER BY state_code) = 1""")
    placed = con.execute("SELECT osm_id, level, country FROM _placed").fetchall()
    kinds = {oid: kind_for(c, lvl) for oid, lvl, c in placed}
    con.register("_k", pa.table({"osm_id": list(kinds), "kind": list(kinds.values())}))

    # County for localities (tells apart the many same-named towns and townships).
    con.execute("DROP TABLE IF EXISTS areas")
    con.execute("""
        CREATE TABLE areas (area_id VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR, admin_level INTEGER,
                            county VARCHAR, state_code VARCHAR, state_name VARCHAR, country VARCHAR,
                            source VARCHAR, geom GEOMETRY)""")
    con.execute("""
        INSERT INTO areas
        SELECT 'osm:' || p.osm_id, k.kind, p.name, p.level, NULL, p.state_code,
               CASE WHEN k.kind = 'state' THEN p.name ELSE p.state_name END,
               p.country, CASE WHEN p.census THEN 'osm-census' ELSE 'osm' END, p.geom
        FROM _placed p JOIN _k k USING (osm_id) WHERE k.kind IS NOT NULL""")
    con.unregister("_k")
    con.execute("""UPDATE areas SET state_code = split_part(i.iso2, '-', 2)
                   FROM _admin i WHERE areas.area_id = 'osm:' || i.osm_id AND areas.kind = 'state'
                   AND i.iso2 IS NOT NULL""")
    con.execute("""
        UPDATE areas SET county = c.name FROM (
            SELECT l.area_id, arg_max(c.name, ST_Area(c.geom)) AS name
            FROM areas l JOIN areas c ON c.kind = 'county' AND l.kind = 'locality'
                 AND ST_Contains(c.geom, ST_PointOnSurface(l.geom))
            GROUP BY l.area_id) c
        WHERE areas.area_id = c.area_id""")
    # A census place named like a municipality in the same county is the same town twice.
    con.execute("""
        DELETE FROM areas WHERE source = 'osm-census' AND EXISTS (
            SELECT 1 FROM areas m WHERE m.source = 'osm' AND m.kind = 'locality' AND m.name = areas.name
              AND m.county IS NOT DISTINCT FROM areas.county AND m.state_code IS NOT DISTINCT FROM areas.state_code)""")
    # Countries cut off by the extract (no level-2 polygon): union of their states.
    con.execute("""
        INSERT INTO areas
        SELECT 'iso:' || s.country, 'country', s.country, 2, NULL, NULL, NULL, s.country, 'osm-states',
               ST_Union_Agg(s.geom)
        FROM _states s
        WHERE s.country NOT IN (SELECT country FROM areas WHERE kind = 'country' AND country IS NOT NULL)
        GROUP BY s.country""")
    for code, name in COUNTRY_NAMES.items():
        con.execute("UPDATE areas SET name = ? WHERE area_id = ?", [name, f"iso:{code}"])

    if zcta_path is None:
        zcta_path = fetch_zcta()
    src = f"/vsizip/{zcta_path}/{zcta_path.stem}.shp" if zcta_path.suffix == ".zip" else str(zcta_path)
    con.execute(f"""
        INSERT INTO areas
        SELECT 'zip:' || ZCTA5CE20, 'zip', ZCTA5CE20, NULL, NULL, NULL, NULL, 'US', 'census-zcta-2020',
               ST_MakeValid(geom)
        FROM ST_Read('{src}')""")
    # Zip -> state: the state polygon containing the ZCTA's inside point (ZCTAs can straddle a line).
    con.execute("""
        UPDATE areas SET state_code = s.state_code, state_name = s.name FROM (
            SELECT z.area_id, arg_max(s.state_code, s.area_id) AS state_code, arg_max(s.name, s.area_id) AS name
            FROM areas z JOIN areas s ON s.kind = 'state' AND z.kind = 'zip'
                 AND ST_Contains(s.geom, ST_PointOnSurface(z.geom))
            GROUP BY z.area_id) s
        WHERE areas.area_id = s.area_id""")
    n_areas = con.execute("SELECT kind, count(*) FROM areas GROUP BY 1 ORDER BY 1").fetchall()
    log(f"  areas: {dict(n_areas)}")

    assign_ways(con)
    stats = {"areas": dict(n_areas), "way_areas": con.execute("SELECT count(*) FROM way_areas").fetchone()[0]}
    if own:
        con.close()
    return stats


def assign_ways(con) -> None:
    """way_areas(way_id, area_id, frac): the share of each driven way's length inside each area. The
    share is measured in degrees along the line itself, which matches the metric share closely at way
    scale. way_info.geometry is the longest driven part of the way."""
    _spatial(con)
    con.execute("DROP TABLE IF EXISTS way_areas")
    con.execute("""
        CREATE TEMP TABLE _ways AS
        SELECT way_id, ST_GeomFromGeoJSON('{"type":"LineString","coordinates":' || geometry || '}') AS line
        FROM way_info WHERE json_array_length(geometry) >= 2""")
    con.execute("""
        CREATE TABLE way_areas AS
        SELECT w.way_id, a.area_id,
               CASE WHEN ST_Length(w.line) = 0 THEN 1.0
                    WHEN ST_Within(w.line, a.geom) THEN 1.0
                    ELSE least(1.0, ST_Length(ST_Intersection(w.line, a.geom)) / ST_Length(w.line)) END AS frac
        FROM _ways w JOIN areas a ON ST_Intersects(w.line, a.geom)""")
    con.execute("DELETE FROM way_areas WHERE frac <= 0")
    con.execute("DROP TABLE _ways")


# ---------------------------------------------------------------- lookup


def _label(a: dict) -> str:
    """'Kailua (Honolulu County, HI)', '96734 (HI)', 'Honolulu County (HI)', 'Hawaii (US)'."""
    where = [x for x in (a.get("county") if a["kind"] == "locality" else None,
                         a.get("state_code") if a["kind"] != "state" else None,
                         a.get("country") if a["kind"] in ("state", "country") or not a.get("state_code") else None) if x]
    if a["kind"] == "country":
        where = []
    return f"{a['name']} ({', '.join(where)})" if where else a["name"]


def find_areas(con, query: str, limit: int = 20) -> list[dict]:
    """Areas matching a zip code, a place name, or 'Name, qualifier' where the qualifier is a state
    code or name, a county, or a country code. Exact names rank before prefix matches; a zip query
    only matches zips. Each result carries driven miles in the area (0 if never driven)."""
    q = query.strip()
    qual = None
    if "," in q:
        q, qual = (s.strip() for s in q.rsplit(",", 1))
    if re.fullmatch(r"\d{5}", q):
        cond, params = "a.kind = 'zip' AND a.name = ?", [q]
    else:
        cond, params = "lower(a.name) LIKE lower(?) || '%'", [q]
    if qual:
        cond += """ AND (lower(a.state_code) = lower(?) OR lower(a.state_name) = lower(?)
                        OR lower(a.county) = lower(?) OR lower(a.county) = lower(?) || ' county'
                        OR lower(a.country) = lower(?))"""
        params += [qual] * 5
    rows = _rows(con.execute(f"""
        SELECT a.area_id, a.kind, a.name, a.county, a.state_code, a.state_name, a.country,
               (lower(a.name) = lower(?)) AS exact,
               coalesce((SELECT sum(t.length_m * wa.frac) FROM way_areas wa JOIN traversals t USING (way_id)
                         WHERE wa.area_id = a.area_id), 0) / {M_PER_MI} AS mi
        FROM areas a WHERE {cond}
        ORDER BY exact DESC, mi DESC, a.name LIMIT ?""", [q] + params + [limit]))
    rows = [r for r in rows if r["exact"]] or rows  # an exact name hides longer ones that start with it
    for r in rows:
        r["label"] = _label(r)
        del r["exact"]
    return rows


def resolve_area(con, query: str) -> tuple[dict | None, list[dict]]:
    """(area, []) when the query names one area, else (None, candidates)."""
    cands = find_areas(con, query)
    return (cands[0], []) if len(cands) == 1 else (None, cands)


def area_summary(con, area_id: str, top_n: int = 10) -> dict | None:
    a = con.execute("""SELECT area_id, kind, name, county, state_code, state_name, country FROM areas
                       WHERE area_id = ?""", [area_id]).fetchone()
    if not a:
        return None
    out = dict(zip(["area_id", "kind", "name", "county", "state_code", "state_name", "country"], a))
    out["label"] = _label(out)
    s = con.execute(f"""
        SELECT sum(t.length_m * wa.frac) / {M_PER_MI}, count(DISTINCT t.drive_id), count(DISTINCT t.date),
               min(t.date), max(t.date)
        FROM way_areas wa JOIN traversals t USING (way_id) WHERE wa.area_id = ?""", [area_id]).fetchone()
    out.update(dict(zip(["mi", "drive_count", "distinct_days", "first_date", "last_date"], s)))
    out["mi"] = out["mi"] or 0.0
    out["top_roads"] = _rows(con.execute(f"""
        SELECT wr.road_key, r.label, count(DISTINCT t.drive_id) AS drive_count,
               sum(t.length_m * wa.frac) / {M_PER_MI} AS mi
        FROM way_areas wa JOIN traversals t USING (way_id) JOIN way_roads wr USING (way_id)
        JOIN roads r USING (road_key)
        WHERE wa.area_id = ?
        GROUP BY wr.road_key, r.label ORDER BY drive_count DESC, mi DESC LIMIT ?""", [area_id, top_n]))
    out["dates"] = _rows(con.execute(f"""
        SELECT t.date, list(DISTINCT t.drive_id) AS drive_ids, count(DISTINCT t.drive_id) AS drives,
               sum(t.length_m * wa.frac) / {M_PER_MI} AS mi
        FROM way_areas wa JOIN traversals t USING (way_id) WHERE wa.area_id = ?
        GROUP BY t.date ORDER BY t.date DESC""", [area_id]))
    if out["kind"] == "zip":
        out["note"] = ZIP_NOTE
    return out


def area_geojson(con, area_id: str, tolerance: float = 0.0002) -> dict | None:
    _spatial(con)
    r = con.execute("SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, ?)) FROM areas WHERE area_id = ?",
                    [tolerance, area_id]).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def area_roads_geojson(con, area_id: str) -> dict:
    """Driven ways in the area (their stored geometry), for the map layer."""
    rows = con.execute("""
        SELECT w.way_id, w.geometry, coalesce(w.name, w.ref) FROM way_areas wa JOIN way_info w USING (way_id)
        WHERE wa.area_id = ?""", [area_id]).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"way_id": str(wid), "name": nm, "inferred": False, "confidence": "observed"},
         "geometry": {"type": "LineString", "coordinates": json.loads(g)}} for wid, g, nm in rows]}


def area_day_geojson(con, area_id: str, date: str) -> dict:
    """That day's drive edges on ways inside the area."""
    rows = con.execute("""
        SELECT e.drive_id, e.geometry, e.inferred, e.confidence
        FROM drive_edges e JOIN drives d USING (drive_id)
        WHERE d.local_date = ?::DATE AND d.match_status IN ('ok', 'partial')
          AND e.way_id IN (SELECT way_id FROM way_areas WHERE area_id = ?)
        ORDER BY e.drive_id, e.seq""", [date, area_id]).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"drive_id": did, "inferred": bool(inf), "confidence": conf},
         "geometry": {"type": "LineString", "coordinates": json.loads(g)}} for did, g, inf, conf in rows]}


def areas_driven(con, kinds=("state", "country"), since: str | None = None, until: str | None = None) -> list[dict]:
    """Areas of the given kinds with any driving in the date range, most miles first."""
    return _rows(con.execute(f"""
        SELECT a.area_id, a.kind, a.name, a.country, sum(t.length_m * wa.frac) / {M_PER_MI} AS mi,
               count(DISTINCT t.drive_id) AS drives
        FROM areas a JOIN way_areas wa USING (area_id) JOIN traversals t USING (way_id)
        WHERE a.kind IN (SELECT unnest(?)) AND (?::DATE IS NULL OR t.date >= ?::DATE)
              AND (?::DATE IS NULL OR t.date <= ?::DATE)
        GROUP BY ALL ORDER BY a.kind, mi DESC""", [list(kinds), since, since, until, until]))
