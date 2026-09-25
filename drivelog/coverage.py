"""Tile coverage from Geofabrik .poly boundary files (fetched into data/coverage/)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import ROOT, Config, load_config
from .valhalla import GEOFABRIK

COVERAGE_DIR = ROOT / "data/coverage"


def _parse_poly(text: str) -> list[tuple[bool, list[tuple[float, float]]]]:
    """Return [(is_hole, [(lng, lat), ...]), ...] rings."""
    rings, cur, hole = [], None, False
    lines = text.splitlines()[1:]  # first line is the name
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s == "END":
            if cur is not None:
                rings.append((hole, cur))
                cur = None
            continue
        if cur is None:
            hole = s.startswith("!")
            cur = []
            continue
        x, y = s.split()[:2]
        cur.append((float(x), float(y)))
    return rings


def _in_ring(lng, lat, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class Coverage:
    def __init__(self, polys: list[list[tuple[bool, list]]]):
        self.polys = []
        for rings in polys:
            xs = [x for _, r in rings for x, _ in r]
            ys = [y for _, r in rings for _, y in r]
            self.polys.append(((min(xs), min(ys), max(xs), max(ys)), rings))

    def contains(self, lat: float, lng: float) -> bool:
        for (x0, y0, x1, y1), rings in self.polys:
            if not (x0 <= lng <= x1 and y0 <= lat <= y1):
                continue
            in_outer = any(_in_ring(lng, lat, r) for hole, r in rings if not hole)
            in_hole = any(_in_ring(lng, lat, r) for hole, r in rings if hole)
            if in_outer and not in_hole:
                return True
        return False


def load_coverage(cfg: Config | None = None) -> Coverage:
    cfg = cfg or load_config()
    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    polys = []
    for extract in cfg.valhalla.get("extracts", []):
        f = COVERAGE_DIR / f"{Path(extract).name}.poly"
        if not f.exists():
            r = httpx.get(f"{GEOFABRIK}/{extract}.poly", follow_redirects=True, timeout=30)
            r.raise_for_status()
            f.write_text(r.text)
        polys.append(_parse_poly(f.read_text()))
    return Coverage(polys)


# ---------------------------------------------------------------- planning new coverage (v2)

INDEX_URL = f"{GEOFABRIK}/index-v1.json"


def extract_path(pbf_url: str) -> str:
    """'https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf' -> 'north-america/us/florida'."""
    return pbf_url.removeprefix(GEOFABRIK + "/").removesuffix("-latest.osm.pbf")


def _ring_area(ring) -> float:
    """Planar area in deg² scaled by cos(lat): only used to rank extracts by size."""
    import math

    lat = sum(y for _, y in ring) / len(ring)
    a = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))
    return abs(a) / 2 * math.cos(math.radians(lat))


@dataclass
class Region:
    id: str
    name: str
    path: str          # extract path as used in config.toml
    tier: str          # country | subdivision | excluded
    area: float
    coverage: Coverage


def region_tier(pid: str, props: dict[str, dict]) -> str:
    """Which Geofabrik extracts the planner may pick. Continents (index roots), the whole US and the US
    census-region bundles (us-northeast, …) are never picked. Countries (children of a root) and their
    first-level subdivisions (US states, Canadian provinces, German Länder, …) are. Deeper subregions
    (e.g. Oberbayern, norcal) are left out, so a trip is covered by the state it's in."""
    p = props[pid]
    parent = p.get("parent")
    if not parent or pid == "us" or (parent == "north-america" and pid.startswith("us-")):
        return "excluded"
    if pid.startswith("us/"):
        return "subdivision"
    if not props.get(parent, {}).get("parent"):
        return "country"
    grand = props[parent].get("parent")
    if grand and not props.get(grand, {}).get("parent"):
        return "subdivision"
    return "excluded"


def load_index(refresh: bool = False) -> list[Region]:
    import json

    f = COVERAGE_DIR / "index-v1.json"
    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    if refresh or not f.exists():
        r = httpx.get(INDEX_URL, follow_redirects=True, timeout=120)
        r.raise_for_status()
        f.write_bytes(r.content)
    feats = json.loads(f.read_text())["features"]
    props = {x["properties"]["id"]: x["properties"] for x in feats}
    out = []
    for x in feats:
        p = x["properties"]
        pbf = (p.get("urls") or {}).get("pbf")
        if not pbf or not x.get("geometry"):
            continue
        polys = []
        for poly in x["geometry"]["coordinates"]:
            polys.append([(i > 0, [tuple(pt[:2]) for pt in ring]) for i, ring in enumerate(poly)])
        area = sum(_ring_area(r) * (-1 if hole else 1) for poly in polys for hole, r in poly)
        out.append(Region(p["id"], p.get("name", p["id"]), extract_path(pbf), region_tier(p["id"], props),
                          area, Coverage(polys)))
    return out


@dataclass
class PlanItem:
    region: Region
    points: int
    drives: set
    size_bytes: int | None = None


@dataclass
class Plan:
    items: list[PlanItem]
    uncovered_points: int
    unresolved_points: int          # outside every allowed extract (e.g. at sea)
    skipped: list[PlanItem]         # extracts that would cover fewer than min_points points
    current: list[str]


def plan_coverage(points: list[tuple[str, float, float]], current: list[str], regions: list[Region],
                  cov: Coverage, min_points: int = 2) -> Plan:
    """points: (drive_id, lat, lng). Each point outside current coverage goes to the smallest allowed
    extract containing it; an extract joins the plan when it covers at least min_points points."""
    allowed = sorted((r for r in regions if r.tier != "excluded" and r.path not in current), key=lambda r: r.area)
    chosen: dict[str, PlanItem] = {}
    uncovered = unresolved = 0
    for did, lat, lng in points:
        if cov.contains(lat, lng):
            continue
        uncovered += 1
        hit = next((r for r in allowed if r.coverage.contains(lat, lng)), None)  # smallest first
        if hit is None:
            unresolved += 1
            continue
        it = chosen.setdefault(hit.id, PlanItem(hit, 0, set()))
        it.points += 1
        it.drives.add(did)
    items = sorted(chosen.values(), key=lambda i: -i.points)
    return Plan([i for i in items if i.points >= min_points], uncovered, unresolved,
                [i for i in items if i.points < min_points], current)


def pbf_size(path: str) -> int | None:
    try:
        r = httpx.head(f"{GEOFABRIK}/{path}-latest.osm.pbf", follow_redirects=True, timeout=30)
        return int(r.headers["content-length"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def drive_points_all(con) -> list[tuple[str, float, float]]:
    return con.execute("SELECT drive_id, lat, lng FROM drive_points ORDER BY drive_id, seq").fetchall()


def make_plan(con, cfg: Config | None = None, refresh_index: bool = False) -> Plan:
    cfg = cfg or load_config()
    current = list(cfg.valhalla.get("extracts", []))
    plan = plan_coverage(drive_points_all(con), current, load_index(refresh_index), load_coverage(cfg))
    for it in plan.items:
        it.size_bytes = pbf_size(it.region.path)
    return plan


def newly_covered(con, old: Coverage, new: Coverage) -> list[str]:
    """Drives that had points outside the old coverage and have more points inside the new one."""
    import json

    rows = con.execute("SELECT drive_id, match_status, match_detail FROM drives").fetchall()
    cands = [d for d, status, detail in rows
             if status == "out_of_coverage" or (json.loads(detail or "{}").get("dropped_uncovered") or 0) > 0]
    pts: dict[str, list] = {}
    for d, lat, lng in con.execute("""SELECT drive_id, lat, lng FROM drive_points
                                      WHERE drive_id IN (SELECT unnest(?))""", [cands]).fetchall():
        pts.setdefault(d, []).append((lat, lng))
    return sorted(d for d, ps in pts.items()
                  if sum(new.contains(*p) for p in ps) > sum(old.contains(*p) for p in ps))
