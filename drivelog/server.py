"""Phase 6: FastAPI backend for the local viewer. Binds to localhost only."""

from __future__ import annotations

import gzip
import json
import subprocess
import threading
from datetime import date

import duckdb
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response

from . import areas, facets, patterns, queries
from .config import ROOT, load_config
from .db import connect

WEB = ROOT / "web"
DIST = WEB / "dist"

app = FastAPI(title="drivelog")
app.add_middleware(GZipMiddleware, minimum_size=1000)

_lock = threading.Lock()
_con = None
_cache: dict[str, bytes] = {}


def con():
    global _con
    if _con is None:
        _con = connect(load_config(), read_only=True)
        try:
            _con.execute("LOAD spatial")  # area boundaries (drivelog.areas)
        except duckdb.Error:
            pass
    return _con.cursor()


_engine = None


def engine() -> facets.Engine:
    """The dashboard's filter engine, loaded once per server process (~0.3 s)."""
    global _engine
    with _lock:
        if _engine is None:
            _engine = facets.Engine(con())
        return _engine


def _json(obj) -> Response:
    return Response(json.dumps(obj, default=_default, separators=(",", ":")), media_type="application/json")


def _default(o):
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def _build_roads() -> tuple[bytes, bytes]:
    """Roads as GeoJSON (feature id = index) and the traversal table the client filters by date.
    Quick mode (no roads matched yet) has neither: both come back empty."""
    c = con()
    if not c.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'road_stats'").fetchone()[0]:
        empty = json.dumps({"type": "FeatureCollection", "features": []}).encode()
        return empty, json.dumps({"days": [], "drive_ids": [], "feature": [], "day": [], "drive": [], "length": [],
                                  "observed": []}).encode()
    ways = c.execute("""
        SELECT s.way_id, s.name, s.ref, s.road_class, s.drive_count, w.geometry,
               (SELECT list(road_key) FROM way_roads wr WHERE wr.way_id = s.way_id) AS keys
        FROM road_stats s JOIN way_info w USING (way_id) ORDER BY s.way_id""").fetchall()
    idx = {}
    feats = []
    for wid, name, ref, rc, dc, geom, keys in ways:
        g = json.loads(geom)
        if len(g) < 2:
            continue
        idx[wid] = len(feats)
        feats.append({
            "type": "Feature", "id": len(feats),
            # 5 decimals ≈ 1 m: plenty for an overlay, and a smaller payload.
            "geometry": {"type": "LineString", "coordinates": [[round(x, 5), round(y, 5)] for x, y in g]},
            "properties": {"way_id": str(wid), "name": name, "ref": ref, "rc": rc, "dc": dc,
                           "road": (keys or [None])[0]},
        })
    roads = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")).encode()

    trav = c.execute("""
        SELECT t.way_id, t.date, t.drive_id, t.length_m, t.observed FROM traversals t ORDER BY t.date""").fetchall()
    days = sorted({t[1] for t in trav})
    day_idx = {d: i for i, d in enumerate(days)}
    drive_ids = sorted({t[2] for t in trav})
    drive_idx = {d: i for i, d in enumerate(drive_ids)}
    f, dy, dr, ln, ob = [], [], [], [], []
    for wid, d, did, length, observed in trav:
        if wid in idx:
            f.append(idx[wid])
            dy.append(day_idx[d])
            dr.append(drive_idx[did])
            ln.append(round(length))
            ob.append(1 if observed else 0)
    tr = json.dumps({"days": [d.isoformat() for d in days], "drive_ids": drive_ids,
                     "feature": f, "day": dy, "drive": dr, "length": ln, "observed": ob},
                    separators=(",", ":")).encode()
    return roads, tr


def _cached(key: str) -> bytes:
    """Gzipped payload, built and compressed once per server process."""
    with _lock:
        if key not in _cache:
            roads, trav = _build_roads()
            _cache["roads"] = gzip.compress(roads, compresslevel=6)
            _cache["traversals"] = gzip.compress(trav, compresslevel=6)
        return _cache[key]


def _gz(key: str) -> Response:
    # Bypasses GZipMiddleware (it skips responses that already have Content-Encoding).
    return Response(_cached(key), media_type="application/json", headers={"Content-Encoding": "gzip"})


@app.get("/api/meta")
def meta():
    c = con()
    r = c.execute(f"""SELECT min(local_date), max(local_date), count(*), sum({queries.DRIVE_M}) / 1000,
                            count(*) FILTER (WHERE match_status IN ('ok', 'partial'))
                     FROM drives""").fetchone()
    # Where the map opens: the middle 96% of drive points, so a one-off trip doesn't zoom it out.
    b = c.execute("""SELECT quantile_cont(lng, 0.02), quantile_cont(lat, 0.02), quantile_cont(lng, 0.98),
                            quantile_cont(lat, 0.98) FROM drive_points""").fetchone()
    e = engine()
    # Quick mode (nothing matched yet): miles are the point-path estimate, as on the dashboard.
    mi = (r[3] or 0) / 1.609344 if e.mode == "full" else e.query({}, facets=False, page_size=1)["totals"]["mi"]
    return _json({"first_date": r[0], "last_date": r[1], "drives": r[2], "mi": mi, "mode": e.mode,
                  "matched": r[4], "bbox": list(b) if b[0] is not None else None})


@app.get("/api/points")
def points():
    """Quick mode's map: GPS points (lng, lat, rounded to ~10 m), at most ~200k of them, for a heat map."""
    c = con()
    n = c.execute("SELECT count(*) FROM drive_points").fetchone()[0]
    step = max(1, n // 200_000)
    rows = c.execute(f"""SELECT round(lng, 4), round(lat, 4) FROM (
                            SELECT lng, lat, row_number() OVER () AS i FROM drive_points) WHERE i % {step} = 0""").fetchall()
    return _json({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [x, y]}} for x, y in rows]})


@app.get("/api/roads")
def roads():
    return _gz("roads")


@app.get("/api/traversals")
def traversals():
    return _gz("traversals")


@app.get("/api/road")
def road(way_id: int | None = None, key: str | None = None, since: str | None = None, until: str | None = None):
    c = con()
    if key is None and way_id is not None:
        r = c.execute("SELECT road_key FROM way_roads WHERE way_id = ? ORDER BY road_key LIMIT 1", [way_id]).fetchone()
        key = r[0] if r else None
    if key is None:
        if way_id is None:
            raise HTTPException(400, "way_id or key required")
        # Unnamed way: stats for just this way.
        w = c.execute("SELECT name, ref, road_class FROM way_info WHERE way_id = ?", [way_id]).fetchone()
        where, params = queries._date_filter(since, until)
        s = c.execute(f"""SELECT count(DISTINCT drive_id), count(DISTINCT drive_id) FILTER (WHERE observed),
                                 count(DISTINCT date), sum(length_m) / {queries.M_PER_MI}, min(date), max(date)
                          FROM traversals t WHERE way_id = ? AND {where}""", [way_id] + params).fetchone()
        rows = c.execute(f"""
            SELECT t.date, list({{'drive_id': t.drive_id, 'start': strftime(t.local_start, '%H:%M'),
                                 'confidence': t.confidence}} ORDER BY t.local_start)
            FROM traversals t WHERE t.way_id = ? AND {where} GROUP BY t.date ORDER BY t.date DESC""",
                         [way_id] + params).fetchall()
        dates = []
        for d, drives in rows:
            conf = "observed" if any(x["confidence"] == "observed" for x in drives) else (
                "high" if any(x["confidence"] == "high" for x in drives) else "low")
            dates.append({"date": d, "starts": [x["start"] for x in drives],
                          "drive_ids": [x["drive_id"] for x in drives], "drives": drives, "confidence": conf})
        return _json({
            "road_key": None, "label": (w[0] or w[1] or f"Unnamed {w[2]}") if w else "Unknown",
            "ref": w[1] if w else None, "names": [], "road_class": w[2] if w else None,
            "drive_count": s[0], "observed_drive_count": s[1], "distinct_days": s[2], "total_mi": s[3],
            "first_date": s[4], "last_date": s[5], "way_ids": [str(way_id)], "dates": dates,
        })
    summary = queries.road_summary(c, key, since, until)
    if summary is None:
        raise HTTPException(404, "road not found")
    summary["way_ids"] = [str(w) for w in summary["way_ids"]]
    summary["dates"] = queries.road_dates(c, key, since, until)
    return _json(summary)


@app.get("/api/search")
def search(q: str = Query(..., min_length=1)):
    """Roads and areas (zip codes, places, counties, states, countries) matching the query."""
    c = con()
    try:
        area_hits = areas.find_areas(c, q, limit=8)
    except duckdb.CatalogException:  # `drivelog areas build` not run yet
        area_hits = []
    return _json({"roads": _road_hits(c, q), "areas": area_hits})


def _road_hits(c, q: str) -> list[dict]:
    rows = queries.find_roads(c, q, limit=10)
    out = []
    for r in rows:
        bbox = c.execute("""
            SELECT list(w.geometry) FROM way_roads wr JOIN way_info w USING (way_id) WHERE wr.road_key = ?""",
                         [r["road_key"]]).fetchone()[0] or []
        xs, ys = [], []
        for g in bbox:
            for x, y in json.loads(g):
                xs.append(x)
                ys.append(y)
        r["bbox"] = [min(xs), min(ys), max(xs), max(ys)] if xs else None
        out.append(r)
    return out


@app.post("/api/query")
def query(body: dict = Body(default={})):
    """Dashboard: totals, charts, lists, records, matching drives and facet counts for a filter set.
    Body: {"filters": {...}, "page": 0, "page_size": 25, "facets": true, "sort": "date", "desc": true}."""
    e = engine()
    r = e.query(body.get("filters"), page=int(body.get("page", 0)), page_size=min(int(body.get("page_size", 25)), 200),
                facets=bool(body.get("facets", True)), sort=str(body.get("sort", "date")), desc=bool(body.get("desc", True)))
    r["labels"] = e.labels(body.get("filters"))
    return _json(r)


@app.post("/api/query/ids")
def query_ids(body: dict = Body(default={})):
    """Drive ids matching a filter set (the map restricts itself to these)."""
    return _json(engine().drive_ids(body.get("filters")))


@app.get("/api/area")
def area(id: str):
    s = areas.area_summary(con(), id)
    if s is None:
        raise HTTPException(404, "area not found")
    return _json(s)


@app.get("/api/area/shape")
def area_shape(id: str):
    g = areas.area_geojson(con(), id)
    if g is None:
        raise HTTPException(404, "area not found")
    return _json({"type": "Feature", "properties": {}, "geometry": g})


@app.get("/api/area/roads")
def area_roads(id: str):
    # Clipping a county's ways to its boundary takes a moment; the result is cached per area.
    key = f"area_roads:{id}"
    if key not in _cache:
        _cache[key] = json.dumps(areas.area_roads_geojson(con(), id), separators=(",", ":")).encode()
    return Response(_cache[key], media_type="application/json")


@app.get("/api/area/day")
def area_day(id: str, date: str):
    return _json(areas.area_day_geojson(con(), id, date))


@app.get("/api/patterns")
def get_patterns(road: str | None = None, area: str | None = None):
    return _json(patterns.patterns(con(), road, area))


@app.get("/api/years")
def get_years():
    return _json(patterns.years(con()))


@app.get("/api/review/{year}")
def get_review(year: int):
    return _json(patterns.review(con(), year))


@app.get("/api/review/{year}/new_roads")
def get_new_roads(year: int):
    return _json(patterns.new_roads_geojson(con(), year))


@app.get("/api/day/{day}")
def day(day: str):
    c = con()
    drives = queries.day_drives(c, day)
    for d in drives:
        d["geometry"] = [json.loads(g) for g in d["geometry"]]
        d["start"] = d["start"].strftime("%H:%M")
    return _json(drives)


@app.get("/api/drive/{drive_id}")
def drive(drive_id: str):
    d = queries.drive_detail(con(), drive_id)
    if d is None:
        raise HTTPException(404, "drive not found")
    return _json(d)


@app.get("/api/drives")
def drives(ids: str):
    """Route geometry for drives, one feature per stored edge, with how each stretch is known."""
    c = con()
    id_list = [i for i in ids.split(",") if i][:500]
    rows = c.execute("""SELECT drive_id, geometry, inferred, confidence FROM drive_edges
                        WHERE drive_id IN (SELECT unnest(?)) ORDER BY drive_id, seq""", [id_list]).fetchall()
    feats = [{"type": "Feature",
              "properties": {"drive_id": did, "inferred": bool(inf), "confidence": conf},
              "geometry": {"type": "LineString", "coordinates": json.loads(g)}}
             for did, g, inf, conf in rows]
    return _json({"type": "FeatureCollection", "features": feats})


@app.get("/{path:path}")
def spa(path: str):
    if not (DIST / "index.html").exists():
        raise HTTPException(404, "Front end not built. Run `drivelog serve` to build it.")
    f = (DIST / path).resolve()
    if path and f.is_file() and f.is_relative_to(DIST.resolve()):
        return FileResponse(f)  # hashed asset names: safe to cache
    # index.html must revalidate, or a rebuilt front end keeps loading the old bundle.
    return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})


def serve(host: str = "127.0.0.1", port: int = 8765, dev: bool = False) -> None:
    import shutil

    import uvicorn

    has_npm = shutil.which("npm") is not None
    if dev or ((not DIST.exists() or _stale()) and has_npm):
        if not (WEB / "node_modules").exists():
            subprocess.run(["npm", "install"], cwd=WEB, check=True)
    if dev:
        subprocess.Popen(["npm", "run", "dev"], cwd=WEB)
        print("Vite dev server: http://localhost:5173 (proxies /api to the backend)")
    elif not DIST.exists() or _stale():
        if has_npm:
            subprocess.run(["npm", "run", "build"], cwd=WEB, check=True)
        elif not DIST.exists():
            raise SystemExit("The viewer isn't built and Node isn't installed: `brew install node`, then run this again.")
        # else: no Node, but the repo ships a built viewer (web/dist): use it as is.
    print("preparing road data…", flush=True)
    _cached("roads")  # build + compress up front so the first page load doesn't pay for it
    print(f"drivelog viewer: http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port)


def _stale() -> bool:
    newest_src = max(p.stat().st_mtime for p in (WEB / "src").rglob("*") if p.is_file())
    return newest_src > (DIST / "index.html").stat().st_mtime
