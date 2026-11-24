"""Filter engine for the dashboard: shopping-site style facets over drives.

A filter set picks values in any of these facets:

  When       from / to (dates), year, month (1-12, across years), dow (0 = Monday), tod (time of day)
  Where      road (road keys), town, zip, county, state, country (area ids)
  The drive  len (length band)

Within a facet the values are OR'd; different facets are AND'd. For each facet, option counts are the
drives that would match with *every other* facet applied, so ticking Canada narrows the province,
town and road lists to Canadian ones, and an option that would empty the results shows 0.

The engine copies the per-drive facts into an in-memory DuckDB once (the viewer's database connection
is read-only), so every query, facet counts included, is a few small SQL statements.

Places per drive live in `drive_places(drive_id, area_id, m)`: metres of the drive inside each area.
With matched roads they come from traversals x way_areas; in quick mode (no routing yet) from the GPS
points themselves (drivelog.quick). A drive's miles are its matched length when matched; in quick mode,
the length of the path through its points (`drives.path_m`), which runs a little low.
"""

from __future__ import annotations

import threading
from typing import Any

import duckdb

from .queries import DRIVE_M, M_PER_MI

TOD = [  # id, label, first hour, end hour
    ("early", "Early morning (12–6 am)", 0, 6),
    ("morning", "Morning (6–10 am)", 6, 10),
    ("midday", "Midday (10 am–3 pm)", 10, 15),
    ("afternoon", "Afternoon (3–7 pm)", 15, 19),
    ("evening", "Evening (7 pm–12 am)", 19, 24),
]
LEN = [  # id, label, from mi, below mi
    ("l1", "Under 1 mi", 0, 1),
    ("l5", "1–5 mi", 1, 5),
    ("l10", "5–10 mi", 5, 10),
    ("l20", "10–20 mi", 10, 20),
    ("l20p", "20 mi or more", 20, None),
]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PLACE_FACETS = {"town": "locality", "zip": "zip", "county": "county", "state": "state", "country": "country"}
SPECIFIC = ["zip", "town", "county", "state", "country"]  # most specific first
FACETS = ["year", "month", "dow", "tod", "road", "town", "zip", "county", "state", "country", "len"]
LIST_LIMIT = 100        # options returned for a long facet (roads, towns, zips); its search box narrows it
MIN_PLACE_M = 150       # a drive "went through" an area when at least this much of it is inside


# ---------------------------------------------------------------- building drive_places


def ensure_path_m(con) -> None:
    """drives.path_m: length of the straight-line path through each drive's points, for drives that
    don't have it yet (the quick-mode mileage)."""
    if not _has(con, "drives", "drive_points"):
        return
    con.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS path_m DOUBLE")
    con.execute("""
        UPDATE drives SET path_m = p.m FROM (
            SELECT drive_id, coalesce(sum(seg), 0) AS m FROM (
                SELECT drive_id,
                       2 * 6371008.8 * asin(sqrt(
                           pow(sin(radians(lat - lag(lat) OVER w) / 2), 2) +
                           cos(radians(lat)) * cos(radians(lag(lat) OVER w)) *
                           pow(sin(radians(lng - lag(lng) OVER w) / 2), 2))) AS seg
                FROM drive_points WINDOW w AS (PARTITION BY drive_id ORDER BY seq))
            GROUP BY drive_id) p
        WHERE drives.drive_id = p.drive_id AND drives.path_m IS NULL""")


def build_places_from_roads(con) -> int:
    """drive_places from matched roads: metres of each drive inside each area (traversals x way_areas).
    Also road_place: a town/state label for road names that repeat, so the filter list can tell them apart."""
    if not _has(con, "traversals", "way_areas", "areas"):
        return 0
    con.execute("""
        CREATE OR REPLACE TABLE drive_places AS
        SELECT t.drive_id, wa.area_id, sum(t.length_m * wa.frac) AS m
        FROM traversals t JOIN way_areas wa USING (way_id)
        GROUP BY 1, 2 HAVING sum(t.length_m * wa.frac) >= ?""", [MIN_PLACE_M])
    if _has(con, "roads", "way_roads"):
        build_road_place(con)
    return con.execute("SELECT count(*) FROM drive_places").fetchone()[0]


def _has(con, *tables: str) -> bool:
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return all(t in have for t in tables)


def build_road_place(con) -> None:
    """road_place(road_key, place): for roads whose label is shared with another road ("Bramblewick
    Street", "Bramblewick Street #2"), the town (else county) and state where most of its driving happened."""
    con.execute("""
        CREATE OR REPLACE TABLE road_place AS
        WITH base AS (SELECT road_key, regexp_replace(label, ' #[0-9]+$', '') AS base FROM roads),
             dup AS (SELECT road_key FROM base WHERE base IN (SELECT base FROM base GROUP BY 1 HAVING count(*) > 1)),
             m AS (SELECT wr.road_key, a.kind, a.name, a.state_code, a.country, sum(t.length_m * wa.frac) AS m
                   FROM dup JOIN way_roads wr USING (road_key) JOIN traversals t USING (way_id)
                   JOIN way_areas wa USING (way_id) JOIN areas a USING (area_id)
                   WHERE a.kind IN ('locality', 'county')
                   GROUP BY ALL)
        SELECT road_key,
               arg_max(name || coalesce(', ' || coalesce(state_code, country), ''),
                       m * CASE WHEN kind = 'locality' THEN 1000 ELSE 1 END) AS place
        FROM m GROUP BY road_key""")


# ---------------------------------------------------------------- the engine


def _in(col: str, vals: list) -> tuple[str, list]:
    return f"{col} IN ({', '.join('?' for _ in vals)})", list(vals)


class Engine:
    """Answers filter queries. Build once per database (Engine(con)); thread-safe for reads."""

    def __init__(self, src):
        self.lock = threading.Lock()
        tables = {r[0] for r in src.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        cols = {r[0] for r in src.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'drives'").fetchall()}
        matched = src.execute("SELECT count(*) FROM drives WHERE match_status IN ('ok', 'partial')").fetchone()[0]
        self.mode = "full" if matched else "quick"
        path = "path_m" if "path_m" in cols else "reported_distance_m"
        mi = f"{DRIVE_M} / {M_PER_MI}" if self.mode == "full" else f"coalesce({path}, reported_distance_m) / {M_PER_MI}"
        tod = " ".join(f"WHEN hour(local_start) < {hi} THEN '{i}'" for i, _, _, hi in TOD)
        band = " ".join(f"WHEN mi < {hi} THEN '{i}'" for i, _, _, hi in LEN if hi is not None)
        m = duckdb.connect()
        drv = src.execute(f"""
            SELECT drive_id, local_date AS date, local_start AS ts, year(local_date) AS year,
                   month(local_date) AS month, isodow(local_start) - 1 AS dow, hour(local_start) AS hour,
                   CASE {tod} END AS tod, coalesce({mi}, 0) AS mi,
                   datediff('second', start_time, end_time) / 60.0 AS minutes
            FROM drives""").arrow()
        m.register("_drv", drv)
        m.execute("CREATE TABLE drv AS SELECT *, CASE " + band + " ELSE 'l20p' END AS len FROM _drv")
        if self.mode == "full" and "road_drives" in tables:
            r = src.execute(f"SELECT drive_id, road_key, length_m / {M_PER_MI} AS mi FROM road_drives").arrow()
            m.register("_r", r)
            m.execute("CREATE TABLE droad AS SELECT * FROM _r")
            labels = src.execute(
                "SELECT r.road_key, r.label, p.place FROM roads r LEFT JOIN road_place p USING (road_key)"
                if "road_place" in tables else "SELECT road_key, label, NULL AS place FROM roads").arrow()
            m.register("_labels", labels)
            m.execute("""CREATE TABLE road_label AS SELECT road_key,
                         regexp_replace(label, ' #[0-9]+$', '') || coalesce(' · ' || place, '') AS label FROM _labels""")
        else:
            m.execute("CREATE TABLE droad (drive_id VARCHAR, road_key VARCHAR, mi DOUBLE)")
            m.execute("CREATE TABLE road_label (road_key VARCHAR, label VARCHAR)")
        if "drive_places" in tables and "areas" in tables:
            p = src.execute(f"""SELECT dp.drive_id, dp.area_id, a.kind, dp.m / {M_PER_MI} AS mi
                                FROM drive_places dp JOIN areas a USING (area_id)""").arrow()
            m.register("_p", p)
            m.execute("CREATE TABLE dplace AS SELECT * FROM _p")
            a = src.execute("""SELECT area_id, kind, name, county, state_code, state_name, country FROM areas
                               WHERE area_id IN (SELECT DISTINCT area_id FROM drive_places)""").arrow()
            m.register("_a", a)
            m.execute("""CREATE TABLE area_label AS SELECT area_id, kind, CASE
                           WHEN kind IN ('locality', 'zip') THEN name || coalesce(' · ' || coalesce(state_code, country), '')
                           WHEN kind = 'county' THEN name || coalesce(', ' || state_code, '')
                           WHEN kind = 'state' THEN name || CASE WHEN country NOT IN ('US') THEN coalesce(' · ' || country, '') ELSE '' END
                           ELSE name END AS label FROM _a""")
        else:
            m.execute("CREATE TABLE dplace (drive_id VARCHAR, area_id VARCHAR, kind VARCHAR, mi DOUBLE)")
            m.execute("CREATE TABLE area_label (area_id VARCHAR, kind VARCHAR, label VARCHAR)")
        self.m = m
        self.years = [r[0] for r in m.execute("SELECT DISTINCT year FROM drv ORDER BY 1").fetchall()]
        self.span = m.execute("SELECT min(date), max(date) FROM drv").fetchone()

    # ------------------------------------------------------------ filters -> SQL

    @staticmethod
    def normalize(f: dict | None) -> dict:
        f = dict(f or {})
        out: dict[str, Any] = {k: [str(v) for v in (f.get(k) or [])] for k in FACETS}
        out["from"] = f.get("from") or None
        out["to"] = f.get("to") or None
        out["search"] = {k: str(v) for k, v in (f.get("search") or {}).items() if v}
        return out

    def _pred(self, key: str, vals: list) -> tuple[str, list]:
        if key in ("year", "month", "dow"):
            return _in(key, [int(v) for v in vals])
        if key in ("tod", "len"):
            return _in(key, vals)
        if key == "road":
            s, p = _in("road_key", vals)
            return f"drive_id IN (SELECT drive_id FROM droad WHERE {s})", p
        s, p = _in("area_id", vals)
        return f"drive_id IN (SELECT drive_id FROM dplace WHERE {s})", p

    def _where(self, f: dict, skip: str | None = None) -> tuple[str, list]:
        parts, params = ["TRUE"], []
        if f["from"]:
            parts.append("date >= ?::DATE")
            params.append(f["from"])
        if f["to"]:
            parts.append("date <= ?::DATE")
            params.append(f["to"])
        for k in FACETS:
            if k != skip and f[k]:
                s, p = self._pred(k, f[k])
                parts.append(s)
                params += p
        return " AND ".join(parts), params

    def _q(self, sql: str, params: list | None = None):
        with self.lock:
            return self.m.execute(sql, params or []).fetchall()

    # ------------------------------------------------------------ facet counts

    def _facet(self, f: dict, key: str) -> dict:
        w, p = self._where(f, skip=key)
        sel = set(f[key])
        if key in ("year", "month", "dow", "tod", "len"):
            counts = {str(k): n for k, n in self._q(f"SELECT {key}, count(*) FROM drv WHERE {w} GROUP BY 1", p)}
            if key == "year":
                opts = [(str(y), str(y)) for y in self.years]
            elif key == "month":
                have = {str(r[0]) for r in self._q("SELECT DISTINCT month FROM drv")}
                opts = [(str(i + 1), n) for i, n in enumerate(MONTHS) if str(i + 1) in have]
            elif key == "dow":
                opts = [(str(i), n) for i, n in enumerate(DAYS)]
            elif key == "tod":
                opts = [(i, lab) for i, lab, _, _ in TOD]
            else:
                opts = [(i, lab) for i, lab, _, _ in LEN]
            options = [{"id": i, "label": lab, "count": counts.get(i, 0), "selected": i in sel} for i, lab in opts]
            return {"key": key, "options": options, "total": len(options)}
        if key == "road":
            src, idcol, lab = "droad x JOIN road_label l USING (road_key)", "x.road_key", "l.label"
        else:
            src = f"dplace x JOIN area_label l USING (area_id)"
            idcol, lab = "x.area_id", "l.label"
        kind_filter = "" if key == "road" else f"AND x.kind = '{PLACE_FACETS[key]}'"
        search = f["search"].get(key)
        s_sql, s_p = ("AND l.label ILIKE ?", [f"%{search}%"]) if search else ("", [])
        rows = self._q(f"""
            SELECT {idcol}, any_value({lab}), count(DISTINCT x.drive_id) AS n
            FROM {src} WHERE x.drive_id IN (SELECT drive_id FROM drv WHERE {w}) {kind_filter} {s_sql}
            GROUP BY 1 ORDER BY n DESC, 2 LIMIT {LIST_LIMIT + len(sel)}""", p + s_p)
        total = self._q(f"""SELECT count(DISTINCT {idcol}) FROM {src}
                            WHERE x.drive_id IN (SELECT drive_id FROM drv WHERE {w}) {kind_filter} {s_sql}""", p + s_p)[0][0]
        options = [{"id": i, "label": lab, "count": n, "selected": i in sel} for i, lab, n in rows]
        missing = sel - {o["id"] for o in options}
        if missing:  # selected but now 0: keep them listed so they can be unticked
            table = "road_label" if key == "road" else "area_label"
            col = "road_key" if key == "road" else "area_id"
            for i, lab in self._q(f"SELECT {col}, label FROM {table} WHERE {_in(col, list(missing))[0]}", list(missing)):
                options.insert(0, {"id": i, "label": lab, "count": 0, "selected": True})
        options.sort(key=lambda o: (not o["selected"], -o["count"], o["label"]))
        return {"key": key, "options": options, "total": max(total, len(options))}

    # ------------------------------------------------------------ the answer

    def query(self, filters: dict | None = None, page: int = 0, page_size: int = 25, facets: bool = True) -> dict:
        f = self.normalize(filters)
        w, p = self._where(f)
        n, mi, minutes, days = self._q(
            f"SELECT count(*), coalesce(sum(mi), 0), coalesce(sum(minutes), 0), count(DISTINCT date) FROM drv WHERE {w}", p)[0]
        totals = {"drives": n, "mi": round(mi, 1), "hours": round(minutes / 60, 1), "days": days}
        if f["road"]:
            s, sp = _in("road_key", f["road"])
            totals["on_road_mi"] = round(self._q(
                f"SELECT coalesce(sum(mi), 0) FROM droad WHERE {s} AND drive_id IN (SELECT drive_id FROM drv WHERE {w})", sp + p)[0][0], 1)
        place_key = next((k for k in SPECIFIC if f[k]), None)
        if place_key:
            s, sp = _in("area_id", f[place_key])
            totals["in_place_mi"] = round(self._q(
                f"SELECT coalesce(sum(mi), 0) FROM dplace WHERE {s} AND drive_id IN (SELECT drive_id FROM drv WHERE {w})", sp + p)[0][0], 1)
        months = [{"month": mo, "mi": round(v or 0, 1), "drives": c or 0} for mo, v, c in self._q(f"""
            WITH span AS (SELECT strftime(range, '%Y-%m') AS month FROM range(
                    date_trunc('month', ?::DATE)::TIMESTAMP, ?::DATE + INTERVAL 1 DAY, INTERVAL 1 MONTH)),
                 m AS (SELECT strftime(date, '%Y-%m') AS month, sum(mi) AS mi, count(*) AS n FROM drv WHERE {w} GROUP BY 1)
            SELECT span.month, m.mi, m.n FROM span LEFT JOIN m USING (month) ORDER BY 1""",
            [self.span[0], self.span[1]] + p)] if self.span[0] else []
        grid = [[0] * 24 for _ in range(7)]
        for dow, hour, c in self._q(f"SELECT dow, hour, count(*) FROM drv WHERE {w} GROUP BY 1, 2", p):
            grid[dow][hour] = c
        top_roads = [{"id": k, "label": lab, "drives": c, "mi": round(m_, 1)} for k, lab, c, m_ in self._q(f"""
            SELECT x.road_key, any_value(l.label), count(DISTINCT x.drive_id) AS c, sum(x.mi)
            FROM droad x JOIN road_label l USING (road_key)
            WHERE x.drive_id IN (SELECT drive_id FROM drv WHERE {w}) GROUP BY 1 ORDER BY c DESC, 2 LIMIT 10""", p)]
        top_towns = [{"id": k, "label": lab, "drives": c, "mi": round(m_, 1)} for k, lab, c, m_ in self._q(f"""
            SELECT x.area_id, any_value(l.label), count(DISTINCT x.drive_id), sum(x.mi) AS s
            FROM dplace x JOIN area_label l USING (area_id)
            WHERE x.kind = 'locality' AND x.drive_id IN (SELECT drive_id FROM drv WHERE {w})
            GROUP BY 1 ORDER BY s DESC LIMIT 10""", p)]
        records = {}
        r = self._q(f"SELECT drive_id, date, strftime(ts, '%H:%M'), mi, minutes FROM drv WHERE {w} ORDER BY mi DESC LIMIT 1", p)
        if r:
            d = r[0]
            records["longest"] = {"drive_id": d[0], "date": d[1].isoformat(), "start": d[2], "mi": round(d[3], 1),
                                  "minutes": round(d[4])}
        r = self._q(f"SELECT date, count(*), sum(mi) AS s FROM drv WHERE {w} GROUP BY 1 ORDER BY s DESC LIMIT 1", p)
        if r:
            records["busiest_day"] = {"date": r[0][0].isoformat(), "drives": r[0][1], "mi": round(r[0][2], 1)}
        rows = self._q(f"""
            SELECT d.drive_id, d.date, strftime(d.ts, '%H:%M'), d.mi, d.minutes,
                   (SELECT list(l.label ORDER BY x.mi DESC)[1:3] FROM dplace x JOIN area_label l USING (area_id)
                    WHERE x.drive_id = d.drive_id AND x.kind = 'locality') AS towns
            FROM drv d WHERE {w} ORDER BY d.ts DESC LIMIT {int(page_size)} OFFSET {int(page) * int(page_size)}""", p)
        drives = [{"drive_id": i, "date": dt.isoformat(), "start": st, "mi": round(m_, 1), "minutes": round(mn),
                   "towns": [t.split(" · ")[0] for t in (towns or [])]} for i, dt, st, m_, mn, towns in rows]
        out = {
            "mode": self.mode, "filters": f, "totals": totals, "months": months, "grid": grid,
            "top_roads": top_roads, "top_towns": top_towns, "records": records,
            "drives": drives, "page": page, "page_size": page_size,
        }
        if facets:
            out["facets"] = {k: self._facet(f, k) for k in FACETS if not (k == "road" and self.mode == "quick")}
        return out

    def drive_ids(self, filters: dict | None = None) -> list[str]:
        f = self.normalize(filters)
        w, p = self._where(f)
        return [r[0] for r in self._q(f"SELECT drive_id FROM drv WHERE {w}", p)]

    def labels(self, filters: dict | None = None) -> list[dict]:
        """[{facet, id, label}] for every selected value (for the active-filter tags)."""
        f = self.normalize(filters)
        out = []
        for k in FACETS:
            for v in f[k]:
                out.append({"facet": k, "id": v, "label": self._label(k, v)})
        return out

    def _label(self, key: str, v: str) -> str:
        if key == "year":
            return v
        if key == "month":
            return MONTHS[int(v) - 1]
        if key == "dow":
            return DAYS[int(v)]
        if key == "tod":
            return next(lab for i, lab, _, _ in TOD if i == v)
        if key == "len":
            return next(lab for i, lab, _, _ in LEN if i == v)
        table, col = ("road_label", "road_key") if key == "road" else ("area_label", "area_id")
        r = self._q(f"SELECT label FROM {table} WHERE {col} = ?", [v])
        return r[0][0] if r else v
