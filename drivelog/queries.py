"""Read-side queries shared by the CLI and the web server. Distances are returned in miles."""

from __future__ import annotations

import json
import re

M_PER_MI = 1609.344


# A drive's miles: the matched road length when the drive is matched, else Google's reported distance.
# Reported distance is often far below where the points went (DEBUG.md, v2 Phase 4), so it's only the
# fallback for drives that couldn't be matched.
DRIVE_M = "(CASE WHEN match_status IN ('ok', 'partial') THEN matched_length_m ELSE reported_distance_m END)"


def mi(meters) -> float:
    return (meters or 0) / M_PER_MI


def _date_filter(since: str | None, until: str | None, col: str = "t.date") -> tuple[str, list]:
    clauses, params = [], []
    if since:
        clauses.append(f"{col} >= ?::DATE")
        params.append(since)
    if until:
        clauses.append(f"{col} <= ?::DATE")
        params.append(until)
    return (" AND ".join(clauses) or "TRUE"), params


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- roads


def top_roads(con, n=25, since=None, until=None, by="road", observed_only=False) -> list[dict]:
    where, params = _date_filter(since, until)
    if observed_only:
        where += " AND t.observed"
    if by == "way":
        sql = f"""
            SELECT t.way_id AS key, coalesce(w.name, w.ref, '(unnamed ' || w.road_class || ')') AS label,
                   w.ref, w.road_class, count(DISTINCT t.drive_id) AS drive_count,
                   count(DISTINCT t.date) AS distinct_days, sum(t.length_m) / {M_PER_MI} AS total_mi,
                   min(t.date) AS first_date, max(t.date) AS last_date
            FROM traversals t JOIN way_info w USING (way_id)
            WHERE {where}
            GROUP BY ALL ORDER BY drive_count DESC, total_mi DESC LIMIT ?"""
    else:
        sql = f"""
            SELECT r.road_key AS key, r.label, r.ref, r.names,
                   count(DISTINCT t.drive_id) AS drive_count, count(DISTINCT t.date) AS distinct_days,
                   sum(t.length_m) / {M_PER_MI} AS total_mi, min(t.date) AS first_date, max(t.date) AS last_date
            FROM traversals t JOIN way_roads USING (way_id) JOIN roads r USING (road_key)
            WHERE {where}
            GROUP BY r.road_key, r.label, r.ref, r.names
            ORDER BY drive_count DESC, total_mi DESC LIMIT ?"""
    return _rows(con.execute(sql, params + [n]))


# Street-type abbreviations people type, expanded to the OSM spelling ("Kailua Rd" -> "Kailua Road").
ABBREV = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue", "blvd": "boulevard", "dr": "drive",
    "ln": "lane", "ct": "court", "pl": "place", "hwy": "highway", "pk": "pike", "pke": "pike",
    "pkwy": "parkway", "cir": "circle", "ter": "terrace", "tpke": "turnpike", "n": "north", "s": "south",
    "e": "east", "w": "west", "mt": "mount",
}


def expand_abbrev(q: str) -> str:
    return " ".join(ABBREV.get(w.lower().rstrip("."), w) for w in q.split())


def find_roads(con, query: str, limit: int = 20) -> list[dict]:
    """Match a road by ref number ("72"), ref ("HI 72", "Route 72") or name ("Kailua Rd").
    Ranked: exact name/ref, then name starting with the query, then name containing it, then roads
    that merely carry the name somewhere (a numbered route often carries a town's street name); ties by drives."""
    from .aggregate import normalize_ref

    q = query.strip()
    qx = expand_abbrev(q)
    params: list
    if re.fullmatch(r"\d{1,4}[A-Za-z]?", q):
        cond = "(regexp_matches(r.road_key, ?))"
        params = [rf"^[A-Z]+ {re.escape(q.upper())}(\s|$)"]
        rank = ""  # ref-number search: all matches are equally exact
        rank_params: list = []
    else:
        norm = normalize_ref(q)
        conds, params = [], []
        if norm:
            conds.append("r.road_key = ?")
            params.append(norm)
            if norm.startswith("SR "):
                # "Route 72" should find HI 72 / US 72 as well.
                conds.append("regexp_matches(r.road_key, ?)")
                params.append(rf"^[A-Z]+ {re.escape(norm.split(' ', 1)[1])}$")
        for term in {q, qx}:
            conds.append("r.label ILIKE ?")
            params.append(f"%{term}%")
            conds.append("list_contains(list_transform(r.names, x -> x ILIKE ?), true)")
            params.append(f"%{term}%")
        cond = "(" + " OR ".join(conds) + ")"
        rank = """CASE WHEN r.road_key = ? OR lower(r.label) IN (lower(?), lower(?)) THEN 0
                       WHEN r.label ILIKE ? OR r.label ILIKE ? THEN 1
                       WHEN r.label ILIKE ? OR r.label ILIKE ? THEN 2
                       ELSE 3 END"""
        rank_params = [norm or "", q, qx, f"{q}%", f"{qx}%", f"%{q}%", f"%{qx}%"]
    return _rows(con.execute(f"""
        SELECT r.road_key, r.label, r.ref, r.names, s.drive_count, s.observed_drive_count,
               s.total_km * 1000 / {M_PER_MI} AS total_mi, s.first_date, s.last_date
        FROM roads r JOIN named_road_stats s USING (road_key)
        WHERE {cond}
        ORDER BY {rank + ", " if rank else ""}s.drive_count DESC LIMIT ?""", params + rank_params + [limit]))


def road_dates(con, road_key: str, since=None, until=None) -> list[dict]:
    """Every date the road was driven, newest first. Each drive carries how we know: 'observed'
    (GPS points on the road) or 'high'/'low' (only a routed gap fill put it there)."""
    where, params = _date_filter(since, until, "rd.date")
    rows = con.execute(f"""
        SELECT rd.date,
               list({{'drive_id': rd.drive_id, 'start': strftime(rd.local_start, '%H:%M'),
                     'confidence': rd.confidence}} ORDER BY rd.local_start) AS drives
        FROM road_drives rd
        WHERE rd.road_key = ? AND {where}
        GROUP BY rd.date ORDER BY rd.date DESC""", [road_key] + params).fetchall()
    out = []
    for date, drives in rows:
        observed = any(d["confidence"] == "observed" for d in drives)
        out.append({
            "date": date,
            "starts": [d["start"] for d in drives],
            "drive_ids": [d["drive_id"] for d in drives],
            "drives": drives,
            "confidence": "observed" if observed else ("high" if any(d["confidence"] == "high" for d in drives) else "low"),
        })
    return out


def road_summary(con, road_key: str, since=None, until=None) -> dict | None:
    where, params = _date_filter(since, until)
    r = con.execute(f"""
        SELECT r.road_key, r.label, r.ref, r.names,
               count(DISTINCT t.drive_id), count(DISTINCT t.drive_id) FILTER (WHERE t.observed),
               count(DISTINCT t.date), sum(t.length_m) / {M_PER_MI},
               min(t.date), max(t.date), list(DISTINCT wr.way_id)
        FROM roads r JOIN way_roads wr USING (road_key)
        LEFT JOIN traversals t ON t.way_id = wr.way_id AND {where}
        WHERE r.road_key = ?
        GROUP BY r.road_key, r.label, r.ref, r.names""", params + [road_key]).fetchone()
    if not r:
        return None
    keys = ["road_key", "label", "ref", "names", "drive_count", "observed_drive_count", "distinct_days",
            "total_mi", "first_date", "last_date", "way_ids"]
    return dict(zip(keys, r))


# ---------------------------------------------------------------- drives


def _segments(edges, new_keys: set[str] | None = None) -> list[dict]:
    """Collapse a drive's edges into road segments in order. Unnamed connectors don't break a run, and
    slivers under 0.06 mi are dropped (then neighbours re-merged), so a road never repeats back-to-back."""
    segs: list[dict] = []
    for _, wid, name, ref, rc, length, geom, keys, inferred, conf in edges:
        key = (keys[0] if keys else None) or name or ref
        if not key:
            continue
        label = re.sub(r" #\d+$", "", key)
        if segs and segs[-1]["label"] == label:
            s = segs[-1]
            s["mi"] += mi(length)
            s["inferred_mi"] += mi(length) if inferred else 0
            if inferred and conf == "low":
                s["low"] = True
        else:
            segs.append({"label": label, "road_key": key, "name": name, "ref": ref, "mi": mi(length),
                         "inferred_mi": mi(length) if inferred else 0, "low": bool(inferred and conf == "low"),
                         "new": bool(new_keys and key in new_keys)})
    out: list[dict] = []
    for s in segs:
        if s["mi"] < 0.06:
            continue
        if out and out[-1]["label"] == s["label"]:
            o = out[-1]
            o["mi"] += s["mi"]
            o["inferred_mi"] += s["inferred_mi"]
            o["low"] = o["low"] or s["low"]
            o["new"] = o["new"] or s["new"]
        else:
            out.append(dict(s))
    for s in out:
        # How we know this segment: observed, or only inferred (and then how confidently).
        s["confidence"] = ("observed" if s["inferred_mi"] <= 0.5 * s["mi"]
                           else ("low" if s["low"] else "high"))
        del s["low"]
    return out


def _drive_edges(con, drive_id: str):
    return con.execute("""
        SELECT e.seq, e.way_id, e.name, e.ref, e.road_class, e.length_m, e.geometry,
               (SELECT list(road_key ORDER BY road_key) FROM way_roads wr WHERE wr.way_id = e.way_id) AS keys,
               e.inferred, e.confidence
        FROM drive_edges e WHERE drive_id = ? ORDER BY seq""", [drive_id]).fetchall()


def day_drives(con, date: str) -> list[dict]:
    drives = con.execute("""
        SELECT drive_id, local_start, reported_distance_m, matched_length_m, match_status
        FROM drives WHERE local_date = ?::DATE ORDER BY local_start""", [date]).fetchall()
    out = []
    for did, start, rep, matched, status in drives:
        edges = _drive_edges(con, did)
        new_keys = {r[0] for r in con.execute("SELECT road_key FROM road_first WHERE drive_id = ?", [did]).fetchall()}
        out.append({
            "drive_id": did, "start": start, "reported_mi": mi(rep), "matched_mi": mi(matched),
            "status": status, "new_roads": len(new_keys),
            "roads": _segments(edges, new_keys),
            "geometry": [g for *_, g, _k, _i, _c in edges],
        })
    return out


def resolve_drive(con, ref: str) -> str | None:
    """A drive id, an id prefix, or 'YYYY-MM-DD [HH:MM]' (nearest start that day)."""
    ref = ref.strip()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?", ref)
    if m:
        date, hh, mm = m.groups()
        rows = con.execute("SELECT drive_id, hour(local_start) * 60 + minute(local_start) FROM drives "
                           "WHERE local_date = ?::DATE ORDER BY local_start", [date]).fetchall()
        if not rows:
            return None
        if hh is None:
            return rows[0][0]
        target = int(hh) * 60 + int(mm)
        return min(rows, key=lambda r: abs(r[1] - target))[0]
    r = con.execute("SELECT drive_id FROM drives WHERE drive_id LIKE ? || '%' LIMIT 2", [ref]).fetchall()
    return r[0][0] if len(r) == 1 else None


def similar_drives(con, drive_id: str, limit: int = 10, min_shared_mi: float = 0.5) -> list[dict]:
    """Other drives ranked by road distance shared with this one (per OSM way, the smaller of the two
    traversal lengths). pct_this = share of this drive's route; pct_other = share of the other's."""
    return _rows(con.execute(f"""
        WITH me AS (
            SELECT way_id, sum(length_m) AS len FROM drive_edges
            WHERE drive_id = ? AND way_id IS NOT NULL GROUP BY way_id),
        me_total AS (SELECT sum(len) AS total FROM me),
        shared AS (
            SELECT e.drive_id, sum(least(e.len, me.len)) AS shared_m
            FROM (SELECT drive_id, way_id, sum(length_m) AS len FROM drive_edges
                  WHERE way_id IN (SELECT way_id FROM me) AND drive_id <> ? GROUP BY drive_id, way_id) e
            JOIN me USING (way_id)
            GROUP BY e.drive_id)
        SELECT s.drive_id, d.local_date AS date, strftime(d.local_start, '%H:%M') AS start,
               s.shared_m / {M_PER_MI} AS shared_mi, d.matched_length_m / {M_PER_MI} AS drive_mi,
               s.shared_m / (SELECT total FROM me_total) AS pct_this,
               s.shared_m / nullif(d.matched_length_m, 0) AS pct_other
        FROM shared s JOIN drives d USING (drive_id)
        WHERE s.shared_m >= ?
        ORDER BY s.shared_m DESC LIMIT ?""", [drive_id, drive_id, min_shared_mi * M_PER_MI, limit]))


def drive_detail(con, drive_id: str) -> dict | None:
    r = con.execute("""
        SELECT drive_id, local_date, local_start, reported_distance_m, matched_length_m, match_status,
               match_detail, n_points, date_diff('minute', start_time, end_time)
        FROM drives WHERE drive_id = ?""", [drive_id]).fetchone()
    if not r:
        return None
    did, date, start, rep, matched, status, detail, n_points, minutes = r
    detail = json.loads(detail or "{}")
    edges = _drive_edges(con, did)
    new_roads = [row[0] for row in con.execute(
        "SELECT road_key FROM road_first WHERE drive_id = ? ORDER BY road_key", [did]).fetchall()]
    new_ways = {row[0] for row in con.execute("SELECT way_id FROM way_first WHERE drive_id = ?", [did]).fetchall()}
    features = []
    for _, wid, name, ref, rc, length, geom, keys, inferred, conf in edges:
        features.append({"type": "Feature",
                         "properties": {"way_id": str(wid) if wid else None, "inferred": bool(inferred),
                                        "confidence": conf, "new": wid in new_ways,
                                        "road": (keys or [None])[0], "name": name or ref},
                         "geometry": {"type": "LineString", "coordinates": json.loads(geom)}})
    return {
        "drive_id": did, "date": date, "start": start.strftime("%H:%M"), "minutes": minutes,
        "status": status, "points": n_points,
        "reported_mi": mi(rep), "matched_mi": mi(matched),
        "inferred_mi": sum(mi(e[5]) for e in edges if e[8]),
        "low_confidence_mi": sum(mi(e[5]) for e in edges if e[8] and e[9] == "low"),
        "new_mi": sum(mi(e[5]) for e in edges if e[1] in new_ways),
        "new_roads": new_roads,
        "roads": _segments(edges, set(new_roads)),
        "geojson": {"type": "FeatureCollection", "features": features},
        "similar": similar_drives(con, did),
    }


def stats_by(con, by: str = "month", since=None, until=None) -> list[tuple]:
    fmt = {"year": "%Y", "month": "%Y-%m", "week": "%G-W%V", "day": "%Y-%m-%d"}[by]
    where, params = _date_filter(since, until, "local_date")
    return con.execute(f"""
        SELECT strftime(local_date, '{fmt}') AS period, count(*) AS drives,
               sum({DRIVE_M}) / {M_PER_MI} AS miles,
               sum(CASE WHEN match_status IN ('ok','partial') THEN 1 ELSE 0 END) AS matched
        FROM drives WHERE {where} GROUP BY period ORDER BY period""", params).fetchall()
