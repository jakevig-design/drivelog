"""Phase 3: map-match drives against the local Valhalla with /trace_attributes.

Matching stores only what the GPS points support (observed edges) plus the gaps between observed
segments. `drivelog.fill` then chooses a route for each gap and marks those edges inferred."""

from __future__ import annotations

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pyarrow as pa

from .config import Config, load_config
from .coverage import Coverage, load_coverage
from .db import connect
from .geo import haversine_m

# Valhalla's default service_limits.trace.max_distance is 200 km; stay under it.
MAX_CHUNK_M = 150_000
# Discontinuities in matched geometry larger than this are recorded as gaps (filled by drivelog.fill).
GAP_MIN_M = 250
# drive_edges.seq layout: observed item k gets k * SEQ_STRIDE; a gap after it gets + GAP_OFFSET, and
# its fill edges + GAP_OFFSET + j. Keeps inferred edges in route order without renumbering.
SEQ_STRIDE = 10_000
GAP_OFFSET = 5_000

ATTRIBUTES = [
    "edge.way_id", "edge.names", "edge.road_class", "edge.length",
    "edge.begin_shape_index", "edge.end_shape_index",
    "matched.type", "matched.edge_index", "matched.distance_from_trace_point",
    "shape",
]

# Valhalla returns route refs ("HI 72", "I 990", "US 991") mixed in with street names.
REF_RE = re.compile(
    r"^(?:I|US|SR|PR|CR|PA|NJ|NY|MD|DE|CT|MA|VT|NH|ME|RI|QC|A|R|TPK|[A-Z]{1,3})[- ]?\d{1,4}[A-Z]?(?:\s?(?:N|S|E|W|Bus|Alt|Byp|Truck|Spur))*$"
)


def is_ref(name: str) -> bool:
    return bool(REF_RE.match(name.strip()))


def decode_polyline6(s: str) -> list[tuple[float, float]]:
    """Return [(lat, lng), ...]."""
    coords, idx, lat, lng = [], 0, 0, 0
    while idx < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        coords.append((lat / 1e6, lng / 1e6))
    return coords


@dataclass
class Edge:
    way_id: int | None
    name: str | None
    ref: str | None
    road_class: str | None
    length_m: float
    geometry: list[list[float]]  # [[lng, lat], ...]
    inferred: bool = False
    confidence: str = "observed"  # observed | high | low


@dataclass
class Gap:
    """A stretch between observed segments. Positions are [lng, lat]; times are None when unknown
    (a break inside one Valhalla response has no trace point on either side)."""
    a: list[float]
    b: list[float]
    a_ts: datetime | None
    b_ts: datetime | None

    @property
    def straight_m(self) -> float:
        return haversine_m(self.a[1], self.a[0], self.b[1], self.b[0])


@dataclass
class PieceResult:
    edges: list[Edge] = field(default_factory=list)
    n_points: int = 0
    n_matched: int = 0
    dists: list[float] = field(default_factory=list)
    error: str | None = None
    first: tuple | None = None  # (ts, lat, lng) of the first / last trace point sent
    last: tuple | None = None


@dataclass
class DriveResult:
    drive_id: str
    status: str
    score: float | None
    matched_length_m: float
    items: list  # Edge | Gap, in route order
    detail: dict

    @property
    def edges(self) -> list[Edge]:
        return [i for i in self.items if isinstance(i, Edge)]

    @property
    def gaps(self) -> list[Gap]:
        return [i for i in self.items if isinstance(i, Gap)]


class Matcher:
    def __init__(self, cfg: Config, coverage: Coverage):
        v = cfg.valhalla
        self.base = v.get("url", "http://localhost:8002")
        self.url = self.base + "/trace_attributes"
        self.opts = {
            "search_radius": v.get("search_radius", 100),
            "gps_accuracy": v.get("gps_accuracy", 50),
            "breakage_distance": v.get("breakage_distance", 5000),
        }
        if "interpolation_distance" in v:
            self.opts["interpolation_distance"] = v["interpolation_distance"]
        self.turn_penalty_factor = v.get("turn_penalty_factor")
        self.coverage = coverage
        self.stationary_m = cfg.stationary_m
        self.client = httpx.Client(timeout=120)

    # ------------------------------------------------------------ request

    def post(self, url: str, body: dict) -> httpx.Response:
        """POST with recovery: if Valhalla drops the connection (e.g. it was restarted), wait for
        /status and retry rather than recording the drive as failed."""
        for attempt in range(3):
            try:
                return self.client.post(url, json=body)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                self._wait_ready()
        raise AssertionError("unreachable")

    def _wait_ready(self, timeout_s: float = 180) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.base + "/status", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(3)

    def request(self, pts: list[tuple[datetime, float, float]], raw: bool = False):
        shape, last_t = [], None
        for ts, lat, lng in pts:
            t = int(ts.timestamp())
            if last_t is not None and t <= last_t:
                t = last_t + 1  # Valhalla wants strictly increasing times
            shape.append({"lat": lat, "lon": lng, "time": t})
            last_t = t
        body = {
            "shape": shape,
            "costing": "auto",
            "shape_match": "map_snap",
            "use_timestamps": True,
            "trace_options": self.opts,
        }
        if self.turn_penalty_factor is not None:
            body["trace_options"]["turn_penalty_factor"] = self.turn_penalty_factor
        if not raw:
            body["filters"] = {"attributes": ATTRIBUTES, "action": "include"}
        return self.post(self.url, body)

    def match_piece(self, pts) -> PieceResult:
        res = PieceResult(n_points=len(pts), first=pts[0], last=pts[-1])
        try:
            r = self.request(pts)
        except httpx.HTTPError as e:
            res.error = f"http: {e}"
            return res
        if r.status_code != 200:
            try:
                err = r.json()
                res.error = f"{err.get('error_code')}: {err.get('error')}"
            except ValueError:
                res.error = f"status {r.status_code}"
            return res
        data = r.json()
        res.edges = self._edges(data)
        for mp in data.get("matched_points", []):
            if mp.get("type") != "unmatched":
                res.n_matched += 1
                if mp.get("distance_from_trace_point") is not None:
                    res.dists.append(mp["distance_from_trace_point"])
        return res

    @staticmethod
    def _edges(data: dict) -> list[Edge]:
        out = []
        shape = decode_polyline6(data.get("shape", ""))
        for e in data.get("edges", []):
            names = e.get("names") or []
            refs = [n for n in names if is_ref(n)]
            streets = [n for n in names if not is_ref(n)]
            b, en = e.get("begin_shape_index", 0), e.get("end_shape_index", 0)
            geom = [[round(lng, 6), round(lat, 6)] for lat, lng in shape[b:en + 1]]
            out.append(Edge(
                way_id=e.get("way_id"),
                name=streets[0] if streets else None,
                ref=";".join(refs) if refs else None,
                road_class=e.get("road_class"),
                length_m=float(e.get("length", 0)) * 1000,  # km by default
                geometry=geom,
            ))
        return out

    def edge_walk(self, encoded_shape: str) -> list[Edge]:
        """Attribute an exact route shape (from /route) with way ids and names."""
        r = self.post(self.url, {
            "encoded_polyline": encoded_shape, "shape_match": "edge_walk", "costing": "auto",
            "filters": {"attributes": ATTRIBUTES, "action": "include"}})
        return self._edges(r.json()) if r.status_code == 200 else []

    # ------------------------------------------------------------ splitting

    def _chunks(self, pts):
        """Split into chunks under MAX_CHUNK_M; consecutive chunks share a point."""
        out, cur, dist = [], [pts[0]], 0.0
        for a, b in zip(pts, pts[1:]):
            d = haversine_m(a[1], a[2], b[1], b[2])
            if dist + d > MAX_CHUNK_M and len(cur) >= 2:
                out.append(cur)
                cur, dist = [a], 0.0
            cur.append(b)
            dist += d
        if len(cur) >= 2:
            out.append(cur)
        return out

    def _split_gaps(self, pts):
        brk = self.opts["breakage_distance"]
        out, cur = [], [pts[0]]
        for a, b in zip(pts, pts[1:]):
            if haversine_m(a[1], a[2], b[1], b[2]) > brk:
                if len(cur) >= 2:
                    out.append(cur)
                cur = []
            cur.append(b)
        if len(cur) >= 2:
            out.append(cur)
        return out

    def _covered_runs(self, pts):
        runs, cur, dropped = [], [], 0
        for p in pts:
            if self.coverage.contains(p[1], p[2]):
                cur.append(p)
            else:
                dropped += 1
                if len(cur) >= 2:
                    runs.append(cur)
                cur = []
        if len(cur) >= 2:
            runs.append(cur)
        return runs, dropped

    # ------------------------------------------------------------ drive

    def match_drive(self, drive_id: str, reported_m: float | None, pts) -> DriveResult:
        # Parked lead-in/tail points add nothing to the route and only give meili noise to snap.
        from .extract import trim_stationary

        pts = trim_stationary(pts, self.stationary_m)
        n = len(pts)
        runs, dropped = self._covered_runs(pts)
        covered = sum(len(r) for r in runs)
        if covered < max(2, 0.5 * n):
            return DriveResult(drive_id, "out_of_coverage", None, 0.0, [],
                               {"points": n, "covered_points": covered})

        pieces: list[PieceResult] = []
        split_used, failed_pieces, errors, pre_split = False, 0, [], 0
        for run in runs:
            # Split at gaps wider than breakage_distance before asking Valhalla: its candidate search
            # across a huge gap can take minutes and exhaust memory (DEBUG.md). drivelog.fill routes
            # across the gaps afterwards.
            gap_pieces = self._split_gaps(run)
            pre_split += len(gap_pieces) - 1
            for chunk in (c for piece in gap_pieces for c in self._chunks(piece)):
                pr = self.match_piece(chunk)
                if pr.error or not pr.edges:
                    errors.append(pr.error)
                    sub = self._split_gaps(chunk)
                    if len(sub) > 1 or (sub and len(sub[0]) < len(chunk)):
                        split_used = True
                        for s in sub:
                            spr = self.match_piece(s)
                            if spr.error or not spr.edges:
                                failed_pieces += 1
                                errors.append(spr.error)
                            else:
                                pieces.append(spr)
                    else:
                        failed_pieces += 1
                else:
                    pieces.append(pr)

        covered_pts = [p for r in runs for p in r]
        items = assemble(pieces, covered_pts[0], covered_pts[-1])
        observed_m = sum(i.length_m for i in items if isinstance(i, Edge))
        n_matched = sum(p.n_matched for p in pieces)
        dists = [d for p in pieces for d in p.dists]
        share = n_matched / n if n else 0.0
        detail = {
            "points": n,
            "matched_points": n_matched,
            "point_share": round(share, 3),
            "mean_dist_m": round(statistics.fmean(dists), 1) if dists else None,
            "dropped_uncovered": dropped,
            "split_used": split_used,
            "gap_splits": pre_split,
            "failed_pieces": failed_pieces,
            "observed_km": round(observed_m / 1000, 3),
            "errors": [e for e in errors if e][:3],
        }
        if not items:
            return DriveResult(drive_id, "failed", 0.0, 0.0, [], detail)
        # Final status/score are set by drivelog.fill once the gaps are routed.
        return DriveResult(drive_id, "unfilled", None, observed_m, items, detail)


def assemble(pieces: list[PieceResult], start_pt: tuple, end_pt: tuple) -> list:
    """Order observed edges and the gaps between them: before the first matched edge (from the drive's
    first point), between pieces, at breaks inside a piece, and after the last edge (to the last point).
    Trace points are (ts, lat, lng); gap times come from the trace points on either side, when known."""
    items: list = []
    pos = [start_pt[2], start_pt[1]]
    ts = start_pt[0]
    for p in pieces:
        run: list[Edge] = []
        for i, e in enumerate(p.edges):
            if not e.geometry:
                continue
            first = e.geometry[0]
            if haversine_m(pos[1], pos[0], first[1], first[0]) > GAP_MIN_M:
                if run:
                    items.extend(merge_edges(run))
                    run = []
                # Only the gap into a piece's first edge has trace times on both sides.
                items.append(Gap(pos, first, ts if i == 0 else None, p.first[0] if i == 0 else None))
            run.append(e)
            pos, ts = e.geometry[-1], None
        items.extend(merge_edges(run))
        if p.edges:
            ts = p.last[0]
    end = [end_pt[2], end_pt[1]]
    if haversine_m(pos[1], pos[0], end[1], end[0]) > GAP_MIN_M:
        items.append(Gap(pos, end, ts, end_pt[0]))
    return items


def merge_edges(edges: list[Edge]) -> list[Edge]:
    """Collapse consecutive Valhalla edges on the same OSM way into one row."""
    out: list[Edge] = []
    for e in edges:
        if (out and e.way_id is not None and out[-1].way_id == e.way_id
                and out[-1].inferred == e.inferred and out[-1].confidence == e.confidence):
            last = out[-1]
            last.length_m += e.length_m
            g = e.geometry[1:] if last.geometry and e.geometry and last.geometry[-1] == e.geometry[0] else e.geometry
            last.geometry.extend(g)
        else:
            out.append(Edge(e.way_id, e.name, e.ref, e.road_class, e.length_m, list(e.geometry),
                            e.inferred, e.confidence))
    return out


# ---------------------------------------------------------------- orchestration


def load_points(con, drive_ids: list[str]) -> dict[str, list]:
    rows = con.execute(
        "SELECT drive_id, ts, lat, lng FROM drive_points WHERE drive_id IN (SELECT unnest(?)) ORDER BY drive_id, seq",
        [drive_ids],
    ).fetchall()
    out: dict[str, list] = {}
    for d, ts, lat, lng in rows:
        out.setdefault(d, []).append((ts.replace(tzinfo=timezone.utc), lat, lng))
    return out


def edges_table(rows: list[tuple[str, int, Edge]]) -> pa.Table:
    return pa.table({
        "drive_id": [d for d, _, _ in rows],
        "seq": pa.array([i for _, i, _ in rows], pa.int32()),
        "way_id": pa.array([e.way_id for _, _, e in rows], pa.int64()),
        "name": [e.name for _, _, e in rows],
        "ref": [e.ref for _, _, e in rows],
        "road_class": [e.road_class for _, _, e in rows],
        "length_m": [e.length_m for _, _, e in rows],
        "geometry": [json.dumps(e.geometry, separators=(",", ":")) for _, _, e in rows],
        "inferred": [e.inferred for _, _, e in rows],
        "confidence": [e.confidence for _, _, e in rows],
    })


def _naive(ts):
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts else None


def save_results(con, results: list[DriveResult]) -> None:
    if not results:
        return
    ids = [r.drive_id for r in results]
    edge_rows, gap_rows = [], []
    for r in results:
        k = 0
        for item in r.items:
            if isinstance(item, Edge):
                k += 1
                edge_rows.append((r.drive_id, k * SEQ_STRIDE, item))
            else:
                g = item
                gap_rows.append((r.drive_id, k * SEQ_STRIDE + GAP_OFFSET, g.a[0], g.a[1], _naive(g.a_ts),
                                 g.b[0], g.b[1], _naive(g.b_ts), g.straight_m))
    con.execute("BEGIN")
    con.execute("DELETE FROM drive_edges WHERE drive_id IN (SELECT unnest(?))", [ids])
    con.execute("DELETE FROM drive_gaps WHERE drive_id IN (SELECT unnest(?))", [ids])
    if edge_rows:
        con.register("_edges", edges_table(edge_rows))
        con.execute("INSERT INTO drive_edges SELECT * FROM _edges")
        con.unregister("_edges")
    if gap_rows:
        cols = ["drive_id", "seq", "a_lng", "a_lat", "a_ts", "b_lng", "b_lat", "b_ts", "straight_m"]
        con.register("_gaps", pa.table({c: [row[i] for row in gap_rows] for i, c in enumerate(cols)}))
        con.execute(f"INSERT INTO drive_gaps ({', '.join(cols)}) SELECT * FROM _gaps")
        con.unregister("_gaps")
    con.executemany(
        "UPDATE drives SET match_status = ?, match_score = ?, matched_length_m = ?, match_detail = ? WHERE drive_id = ?",
        [(r.status, r.score, r.matched_length_m, json.dumps(r.detail), r.drive_id) for r in results],
    )
    con.execute("COMMIT")


def run(rematch: bool = False, drive_ids: list[str] | None = None, cfg: Config | None = None,
        progress: bool = True) -> dict:
    cfg = cfg or load_config()
    con = connect(cfg)
    if drive_ids is None:
        where = "" if rematch else "WHERE match_status = 'pending'"
        drive_ids = [r[0] for r in con.execute(f"SELECT drive_id FROM drives {where} ORDER BY start_time").fetchall()]
    reported = dict(con.execute("SELECT drive_id, reported_distance_m FROM drives").fetchall())
    matcher = Matcher(cfg, load_coverage(cfg))
    workers = cfg.valhalla.get("workers", 4)
    counts: dict[str, int] = {}
    batch_size = 200
    done = 0
    for start in range(0, len(drive_ids), batch_size):
        batch = drive_ids[start:start + batch_size]
        pts = load_points(con, batch)
        results = []
        with ThreadPoolExecutor(workers) as pool:
            futs = [pool.submit(matcher.match_drive, d, reported.get(d), pts.get(d, [])) for d in batch if len(pts.get(d, [])) >= 2]
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                counts[r.status] = counts.get(r.status, 0) + 1
        save_results(con, results)
        done += len(batch)
        if progress:
            print(f"  matched {done:,}/{len(drive_ids):,}  {counts}", flush=True)
    con.close()
    # Route the gaps of everything just matched (and anything left unfilled by an interrupted run).
    from . import fill

    return fill.run(cfg=cfg, progress=progress)
