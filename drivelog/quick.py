"""Quick mode: a dashboard straight from the export, with no routing engine (no Docker).

`drivelog start` reads the export, stores the drives, works out which places each drive went through
from its GPS points alone, and opens the dashboard. Nothing is map-matched, so:

  * miles are the length of the path through each drive's points (drives.path_m), a little low
    because the points are sparse and the path cuts corners;
  * places come from boundary files instead of the OSM extracts: Census places, counties and ZCTAs
    for the US, Natural Earth provinces/states and countries for everywhere (so Canada filters by
    province, not town);
  * road-based features (top roads, road filters, Explored, Year in review, the road map) stay locked
    until road detail is added: `drivelog coverage apply` + `drivelog match` + `drivelog areas build`
    upgrade the same database in place.

A drive "went through" an area when at least MIN_PLACE_M of the path between its points has its
midpoints inside it (the same 150 m rule the matched-roads version uses).
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from .areas import BOUNDARIES, fetch_zcta
from .facets import MIN_PLACE_M, ensure_path_m

SOURCES = {
    # name: (url, shapefile inside the zip)
    "places": ("https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip", "cb_2023_us_place_500k.shp"),
    "counties": ("https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip", "cb_2023_us_county_500k.shp"),
    "admin1": ("https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip",
               "ne_10m_admin_1_states_provinces.shp"),
    "admin0": ("https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip",
               "ne_10m_admin_0_countries.shp"),
}


def fetch(name: str) -> Path:
    """Download a boundary file once into data/boundaries/, through a .part file, and check it's a
    readable zip containing the expected shapefile before keeping it."""
    url, shp = SOURCES[name]
    BOUNDARIES.mkdir(parents=True, exist_ok=True)
    dest = BOUNDARIES / url.rsplit("/", 1)[1]
    if dest.exists():
        return dest
    tmp = dest.with_suffix(".part")
    subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-delay", "3", "-o", str(tmp), url], check=True)
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad or shp not in z.namelist():
            tmp.unlink()
            raise SystemExit(f"{url}: download is damaged ({bad or 'missing ' + shp}); run again")
    tmp.rename(dest)
    return dest


def _src(zip_path: Path, shp: str) -> str:
    return f"/vsizip/{zip_path}/{shp}"


def build_areas(con, paths: dict[str, Path] | None = None, zcta: Path | None = None) -> dict:
    """The areas table from boundary files (replaces any existing one). Paths overridable for tests."""
    paths = paths or {k: fetch(k) for k in SOURCES}
    zcta = zcta or fetch_zcta()
    con.execute("INSTALL spatial; LOAD spatial")
    con.execute("DROP TABLE IF EXISTS areas")
    con.execute("""CREATE TABLE areas (area_id VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR, admin_level INTEGER,
                   county VARCHAR, state_code VARCHAR, state_name VARCHAR, country VARCHAR, source VARCHAR,
                   geom GEOMETRY)""")
    p = {k: _src(v, SOURCES[k][1]) for k, v in paths.items()}
    con.execute(f"""INSERT INTO areas SELECT 'ne0:' || iso, 'country', name, 2, NULL, NULL, NULL, iso,
                    'naturalearth-admin0', ST_MakeValid(geom) FROM (
                      SELECT CASE WHEN ISO_A2 = '-99' THEN ISO_A2_EH ELSE ISO_A2 END AS iso,
                             coalesce(NAME_EN, NAME) AS name, geom FROM ST_Read('{p["admin0"]}'))
                    WHERE iso IS NOT NULL AND iso <> '-99'
                    QUALIFY row_number() OVER (PARTITION BY iso ORDER BY name) = 1""")
    con.execute(f"""INSERT INTO areas SELECT 'ne1:' || adm1_code, 'state', coalesce(name_en, name), 4, NULL,
                    nullif(split_part(iso_3166_2, '-', 2), ''), coalesce(name_en, name), iso_a2,
                    'naturalearth-admin1', ST_MakeValid(geom)
                    FROM ST_Read('{p["admin1"]}') WHERE adm1_code IS NOT NULL
                    QUALIFY row_number() OVER (PARTITION BY adm1_code ORDER BY name) = 1""")
    con.execute(f"""INSERT INTO areas SELECT 'ccounty:' || GEOID, 'county', NAMELSAD, 6, NULL, STUSPS, STATE_NAME,
                    'US', 'census-county-2023', ST_MakeValid(geom) FROM ST_Read('{p["counties"]}')""")
    con.execute(f"""INSERT INTO areas SELECT 'cplace:' || GEOID, 'locality', NAME, 8, NULL, STUSPS, STATE_NAME,
                    'US', 'census-place-2023', ST_MakeValid(geom) FROM ST_Read('{p["places"]}')""")
    zsrc = _src(zcta, zcta.stem + ".shp") if zcta.suffix == ".zip" else str(zcta)
    con.execute(f"""INSERT INTO areas SELECT 'zip:' || ZCTA5CE20, 'zip', ZCTA5CE20, NULL, NULL, NULL, NULL, 'US',
                    'census-zcta-2020', ST_MakeValid(geom) FROM ST_Read('{zsrc}')""")
    # A town's county (tells apart same-named towns), and a zip's state, from a point inside it.
    con.execute("""
        UPDATE areas SET county = c.name FROM (
            SELECT l.area_id, arg_max(c.name, ST_Area(c.geom)) AS name
            FROM areas l JOIN areas c ON c.kind = 'county' AND l.kind = 'locality'
                 AND ST_Contains(c.geom, ST_PointOnSurface(l.geom))
            GROUP BY l.area_id) c
        WHERE areas.area_id = c.area_id""")
    con.execute("""
        UPDATE areas SET state_code = s.code, state_name = s.name FROM (
            SELECT z.area_id, arg_max(s.state_code, s.area_id) AS code, arg_max(s.name, s.area_id) AS name
            FROM areas z JOIN areas s ON s.kind = 'state' AND s.country = 'US' AND z.kind = 'zip'
                 AND ST_Contains(s.geom, ST_PointOnSurface(z.geom))
            GROUP BY z.area_id) s
        WHERE areas.area_id = s.area_id""")
    return dict(con.execute("SELECT kind, count(*) FROM areas GROUP BY 1 ORDER BY 1").fetchall())


def places_from_points(con, min_m: float = MIN_PLACE_M) -> int:
    """drive_places from GPS points: each step between consecutive points is credited, by its length,
    to the areas containing its midpoint."""
    con.execute("LOAD spatial")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _seg AS
        SELECT drive_id, ST_Point((lng + prev_lng) / 2, (lat + prev_lat) / 2) AS mid,
               2 * 6371008.8 * asin(sqrt(pow(sin(radians(lat - prev_lat) / 2), 2) +
                   cos(radians(lat)) * cos(radians(prev_lat)) * pow(sin(radians(lng - prev_lng) / 2), 2))) AS m
        FROM (SELECT drive_id, lat, lng, lag(lat) OVER w AS prev_lat, lag(lng) OVER w AS prev_lng
              FROM drive_points WINDOW w AS (PARTITION BY drive_id ORDER BY seq))
        WHERE prev_lat IS NOT NULL""")
    con.execute("""
        CREATE OR REPLACE TABLE drive_places AS
        SELECT s.drive_id, a.area_id, sum(s.m) AS m
        FROM _seg s JOIN areas a ON ST_Contains(a.geom, s.mid)
        GROUP BY 1, 2 HAVING sum(s.m) >= ?""", [min_m])
    con.execute("DROP TABLE _seg")
    return con.execute("SELECT count(*) FROM drive_places").fetchone()[0]


def prepare(cfg, progress: bool = True) -> dict:
    """Extract new drives and, when nothing is road-matched yet, set up quick-mode places."""
    from . import extract
    from .db import connect

    log = (lambda *a: print(*a, flush=True)) if progress else (lambda *a: None)
    log("[1/3] reading your export")
    s = extract.run(cfg)
    log(f"      {s['drives']:,} drives ({s['inserted']:,} new)")
    con = connect(cfg)
    ensure_path_m(con)
    matched = con.execute("SELECT count(*) FROM drives WHERE match_status IN ('ok', 'partial')").fetchone()[0]
    if matched:
        log("[2/3] road detail is set up: using matched roads")
        con.close()
        return {"mode": "full", **s}
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    have_quick = "areas" in tables and con.execute(
        "SELECT count(*) FROM areas WHERE source LIKE 'naturalearth%'").fetchone()[0] > 0
    if not have_quick:
        log("[2/3] place boundaries (first run downloads ~60 MB of Census and Natural Earth files)")
        log(f"      {build_areas(con)}")
    else:
        log("[2/3] place boundaries already loaded")
    log("      matching drives to places from their GPS points")
    n = places_from_points(con)
    log(f"      {n:,} drive-place links")
    con.close()
    return {"mode": "quick", **s}
