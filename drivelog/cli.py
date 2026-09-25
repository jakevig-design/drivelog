"""`drivelog` command line."""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import connect


def _mi(v) -> str:
    return f"{v:,.1f}" if v is not None else "–"


# How a road use is known: GPS points on it, or only a routed gap fill (high/low confidence).
MARK = {"observed": "", "high": " ~", "low": " ?"}
LEGEND = "  ~ inferred from a routed gap (time fits)   ? inferred, low confidence"


def cmd_top(a):
    from .queries import top_roads

    con = connect(read_only=True)
    rows = top_roads(con, a.n, a.since, a.until, a.by, a.observed_only)
    print(f"{'#':>3}  {'Road':<40} {'Drives':>7} {'Days':>6} {'mi':>9}  First       Last")
    for i, r in enumerate(rows, 1):
        label = r["label"] or str(r["key"])
        if a.by == "road" and r.get("ref") and r.get("names"):
            label = f"{r['label']} ({r['names'][0]})"
        elif a.by == "way":
            label = f"{label} [{r['key']}]"
        print(f"{i:>3}  {label[:40]:<40} {r['drive_count']:>7,} {r['distinct_days']:>6,} "
              f"{_mi(r['total_mi']):>9}  {r['first_date']}  {r['last_date']}")
    if not a.observed_only:
        print("\n(counts include roads inferred across gaps; --observed-only for GPS-confirmed only)")


def cmd_dates(a):
    from .queries import find_roads, road_dates

    con = connect(read_only=True)
    roads = find_roads(con, a.road)
    if not roads:
        print(f"No road matches {a.road!r}.")
        return 1
    any_inferred = False
    for r in roads[: a.roads]:
        rows = road_dates(con, r["road_key"], a.since, a.until)
        alias = f" ({', '.join(r['names'][:2])})" if r["names"] and r["names"][0] != r["label"] else ""
        print(f"\n{r['road_key']}{alias}: {len(rows)} days, {r['drive_count']} drives "
              f"({r['observed_drive_count']} GPS-confirmed), {_mi(r['total_mi'])} mi")
        for row in rows[: a.limit] if a.limit else rows:
            starts = ", ".join(d["start"] + MARK[d["confidence"]] for d in row["drives"])
            any_inferred = any_inferred or row["confidence"] != "observed" or "~" in starts or "?" in starts
            print(f"  {row['date']}  {starts}")
        if a.limit and len(rows) > a.limit:
            print(f"  … {len(rows) - a.limit} more (use --limit 0 for all)")
    if len(roads) > a.roads:
        others = ", ".join(r["road_key"] for r in roads[a.roads:])
        print(f"\nOther matches: {others}")
    if any_inferred:
        print("\n" + LEGEND)


def _road_line(roads) -> str:
    parts = []
    for s in roads:
        tag = MARK[s["confidence"]] + (" *new*" if s.get("new") else "")
        parts.append(f"{s['label']} ({s['mi']:.1f}){tag}")
    return " → ".join(parts) if parts else "(not matched)"


def cmd_day(a):
    from .queries import day_drives

    con = connect(read_only=True)
    drives = day_drives(con, a.date)
    if not drives:
        print(f"No drives on {a.date}.")
        return 1
    for d in drives:
        new = f"  {d['new_roads']} new road{'s' if d['new_roads'] != 1 else ''}" if d["new_roads"] else ""
        print(f"\n{d['start']:%H:%M}  {d['matched_mi']:.1f} mi  [{d['status']}]{new}   id {d['drive_id'][:8]}")
        print("  " + _road_line(d["roads"]))
    print("\n" + LEGEND + "   *new* first time on this road")


def cmd_drive(a):
    from .queries import drive_detail, resolve_drive

    con = connect(read_only=True)
    did = resolve_drive(con, a.drive)
    if not did:
        print(f"No drive matches {a.drive!r} (use an id, id prefix, or 'YYYY-MM-DD HH:MM').")
        return 1
    d = drive_detail(con, did)
    print(f"{d['date']} {d['start']}  {d['matched_mi']:.1f} mi, {d['minutes']} min  [{d['status']}]   id {did}")
    if d["inferred_mi"] >= 0.1:
        print(f"  {d['inferred_mi']:.1f} mi inferred across gaps ({d['low_confidence_mi']:.1f} mi low confidence)")
    print("\nRoute:\n  " + _road_line(d["roads"]))
    if d["new_roads"]:
        print(f"\nNew roads ({d['new_mi']:.1f} mi of road you'd never driven before): {', '.join(d['new_roads'])}")
    if d["similar"]:
        print("\nSimilar drives (by shared road):")
        print(f"  {'Date':<10} {'Start':<5} {'Shared mi':>9} {'% of this':>9} {'% of that':>9}  id")
        for s in d["similar"]:
            print(f"  {s['date']} {s['start']:<5} {s['shared_mi']:>9.1f} {s['pct_this']:>8.0%} "
                  f"{(s['pct_other'] or 0):>9.0%}  {s['drive_id'][:8]}")
    print("\n" + LEGEND + "   *new* first time on this road")


def cmd_stats(a):
    from .queries import stats_by

    con = connect(read_only=True)
    print(f"{a.by.capitalize():<10} {'Drives':>7} {'mi':>10} {'Matched':>8}")
    for period, n, miles, matched in stats_by(con, a.by, a.since, a.until):
        print(f"{period:<10} {n:>7,} {_mi(miles):>10} {matched:>8,}")


def cmd_extract(a):
    from .extract import run

    s = run()
    print(f"Drives: {s['drives']:,}  total {s['total_km'] / 1.609344:,.0f} mi  "
          f"(source driving total {s['source_driving_km'] / 1.609344:,.0f} mi)")
    print(f"New this run: {s['inserted']:,}  already stored: {s['skipped_existing']:,}  "
          f"dropped <500 m: {s['dropped_short']:,}  dropped <2 pts: {s['dropped_points']:,}")


def cmd_match(a):
    from .match import run

    print(run(rematch=a.rematch))


def cmd_fill(a):
    from .fill import run

    print(run(refill=a.refill))


def cmd_validate(a):
    from .validate import run

    run()


def cmd_aggregate(a):
    from .aggregate import run

    print(run())


def cmd_rebuild(a):
    from . import aggregate, extract, match

    print("[1/3] extract")
    s = extract.run()
    print(f"      {s['inserted']:,} new drives ({s['drives']:,} total)")
    print("[2/3] match + fill (new drives only)")
    print(f"      {match.run(rematch=False)}")
    print("[3/3] aggregate")
    print(f"      {aggregate.run()}")


def cmd_valhalla(a):
    from .valhalla import configure, fetch_extracts, health, merge_extracts, write_compose

    if a.action == "configure":
        changes = configure()
        print("\n".join(changes) if changes else "valhalla.json already configured")
        if changes:
            print("restart Valhalla to apply: docker compose restart")
    elif a.action == "fetch":
        cfg = load_config()
        print(f"merged into {merge_extracts(fetch_extracts(cfg), cfg)}")
    elif a.action == "compose":
        write_compose()
        print("wrote docker-compose.yml")
    else:
        print(health())


def cmd_area(a):
    from .areas import area_summary, resolve_area

    con = connect(read_only=True)
    con.execute("LOAD spatial")
    area, cands = resolve_area(con, a.query)
    if area is None:
        if not cands:
            print(f"No zip code or place matches {a.query!r}.")
        else:
            c = cands[0]
            # Suggest the qualifier that actually tells the candidates apart.
            same_state = len({x["state_code"] for x in cands}) == 1
            qual = (c["county"] if same_state and c["county"] else None) or c["state_code"] or c["country"]
            print(f"{a.query!r} matches {len(cands)} areas. Add a state, county or country, e.g. "
                  f"\"{c['name']}, {qual}\":")
            for c in cands:
                print(f"  {c['label']:<50} {c['kind']:<9} {_mi(c['mi']):>9} mi")
        return 1
    s = area_summary(con, area["area_id"])
    print(f"{s['label']}  [{s['kind']}]")
    if s.get("note"):
        print(f"  ({s['note']})")
    if not s["drive_count"]:
        print("  Never driven here.")
        return 0
    print(f"  {_mi(s['mi'])} mi · {s['drive_count']:,} drives · {s['distinct_days']:,} days · "
          f"{s['first_date']} → {s['last_date']}")
    print("\nTop roads here:")
    for r in s["top_roads"]:
        print(f"  {r['label'][:40]:<40} {r['drive_count']:>6,} drives {_mi(r['mi']):>8} mi")
    rows = s["dates"][: a.limit] if a.limit else s["dates"]
    print(f"\nDates driven ({len(s['dates']):,}, newest first):")
    for d in rows:
        print(f"  {d['date']}  {d['drives']} drive{'s' if d['drives'] != 1 else ''}  {_mi(d['mi'])} mi")
    if a.limit and len(s["dates"]) > a.limit:
        print(f"  … {len(s['dates']) - a.limit} more (use --limit 0 for all)")


def cmd_areas(a):
    from .areas import build

    print(build())


SHADES = " ░▒▓█"


def _scope_args(con, a) -> tuple[str | None, str | None, str] | None:
    """Resolve --road / --area to keys; returns (road_key, area_id, label) or None after printing why."""
    if a.road:
        from .queries import find_roads

        roads = find_roads(con, a.road)
        if not roads:
            print(f"No road matches {a.road!r}.")
            return None
        return roads[0]["road_key"], None, roads[0]["road_key"]
    if a.area:
        from .areas import resolve_area

        con.execute("LOAD spatial")
        area, cands = resolve_area(con, a.area)
        if area is None:
            print(f"{a.area!r}: " + ("no match." if not cands else "ambiguous: " + "; ".join(c["label"] for c in cands)))
            return None
        return None, area["area_id"], area["label"]
    return None, None, "all drives"


def cmd_patterns(a):
    from .patterns import patterns

    con = connect(read_only=True)
    scope = _scope_args(con, a)
    if scope is None:
        return 1
    road, area, label = scope
    p = patterns(con, road, area)
    unit = "mi" if a.miles else "drives"
    grid = p["grid"][unit]
    top = max(max(r) for r in grid) or 1
    print(f"{label}: {p['drives']:,} drives · {_mi(p['mi'])} mi · {p['distinct_days']:,} days (local time)\n")
    print(f"{unit} by hour of day × day of week (█ = {_mi(top) if a.miles else f'{top:,}'})")
    print("      " + "".join(f"{h:<3}" if h % 3 == 0 else "   " for h in range(24)))
    for day, row in zip(p["days"], grid):
        cells = "".join(SHADES[max(1, min(4, round(v / top * 4)))] * 2 + " " if v else " · " for v in row)
        print(f"  {day} {cells}")
    print(f"\n{unit} per month:")
    by_year: dict[str, list] = {}
    for m in p["months"]:
        by_year.setdefault(m["month"][:4], [None] * 12)[int(m["month"][5:]) - 1] = m[unit]
    print("        " + "".join(f"{x:>7}" for x in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]))
    for y, vals in by_year.items():
        print(f"  {y}  " + "".join(f"{'' if v is None else (f'{v:,.0f}' if a.miles else f'{v:,}'):>7}" for v in vals))
    print("\nDrive length (whole drive, mi):")
    most = max(b["drives"] for b in p["lengths"]) or 1
    for b in p["lengths"]:
        print(f"  {b['label']:>7}  {'█' * round(b['drives'] / most * 40):<40} {b['drives']:,}")


def _chg(v) -> str:
    return "" if v is None else f" ({v:+.0%} vs prev)"


def cmd_review(a):
    from .patterns import review, years

    con = connect(read_only=True)
    ys = years(con) if a.year == "all" else [int(a.year)]
    for y in ys:
        r = review(con, y)
        t, c = r["totals"], r["change"]
        print(f"\n==== {y} ====")
        print(f"  {_mi(t['mi'])} mi{_chg(c['mi'])} · {t['drives']:,} drives{_chg(c['drives'])} · "
              f"{t['hours']:,.0f} h in the car{_chg(c['hours'])} · {t['days']:,} days driving{_chg(c['days'])}")
        print(f"  New roads: {r['new_roads']['count']:,} ({_mi(r['new_roads']['mi'])} mi)")
        if r["busiest_month"]:
            print(f"  Busiest month: {r['busiest_month']['month']} ({_mi(r['busiest_month']['mi'])} mi, "
                  f"{r['busiest_month']['drives']} drives)")
            print(f"  Busiest day: {r['busiest_day']['date']} ({_mi(r['busiest_day']['mi'])} mi, "
                  f"{r['busiest_day']['drives']} drives)")
            ld = r["longest_drive"]
            print(f"  Longest drive: {ld['date']} {ld['start']}, {_mi(ld['mi'])} mi, {ld['minutes']} min  "
                  f"(drivelog drive {ld['drive_id'][:8]})")
        if r["places"] is not None:
            for kind in ("country", "state"):
                names = [f"{p['name']} ({_mi(p['mi'])})" for p in r["places"] if p["kind"] == kind]
                if names:
                    print(f"  {'Countries' if kind == 'country' else 'States/provinces'}: {', '.join(names)}")
        if r["top_roads"]:
            print("  Top roads: " + ", ".join(f"{x['label']} ({x['drive_count']})" for x in r["top_roads"]))


def _gb(n) -> str:
    return f"{n / 1e9:,.2f} GB" if n is not None else "?"


# Disk model, measured on a 19-region build (11.2 GB of source extracts): per source byte, the merged
# PBF is 1.02x, the tile folder and the tile archive the container serves 1.25x each, so the steady
# state is ~4.5x the sources. A rebuild keeps the previous build for rollback, and the build itself
# writes temporary files of ~3.2x the sources until it finishes. Predicted +44.1 GB for that build;
# it grew +44.2 GB.
STEADY_PER_SRC_BYTE = 4.5
BUILD_TEMP_PER_SRC_BYTE = 3.2
BUILT = ("valhalla_tiles", "valhalla_tiles.tar", "admins.sqlite")


def _size(path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size if path.is_file() else sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def disk_estimate(vdir, new_bytes: int) -> tuple[int, int]:
    """(extra bytes once finished, extra bytes at the peak of the tile build) for adding new_bytes of
    extracts to the Valhalla data under vdir."""
    current = _size(vdir)
    src_after = _size(vdir / "src") + new_bytes
    keep_previous = sum(_size(vdir / b) for b in BUILT)  # moved to previous/, replacing any older copy
    final = STEADY_PER_SRC_BYTE * src_after + keep_previous + _size(vdir / "timezones.sqlite")
    extra = int(final - current)
    return extra, int(extra + BUILD_TEMP_PER_SRC_BYTE * src_after)


def print_plan(plan) -> None:
    print(f"Drive points outside current coverage: {plan.uncovered_points:,}")
    if not plan.items:
        print("Nothing to add.")
    else:
        print(f"\n  {'Extract':<40} {'Kind':<12} {'Points':>7} {'Drives':>7} {'Download':>11}")
        for it in plan.items:
            print(f"  {it.region.path:<40} {it.region.tier:<12} {it.points:>7,} {len(it.drives):>7,} {_gb(it.size_bytes):>11}")
        total = sum(it.size_bytes or 0 for it in plan.items)
        extra, peak = disk_estimate(load_config().valhalla_dir, total)
        print(f"\n  Download: {_gb(total)}   extra disk when done: ~{_gb(extra)} "
              f"(~{_gb(peak)} at the peak of the tile build)")
    if plan.skipped:
        print(f"\nSkipped (fewer than 2 points each): " + ", ".join(f"{i.region.path} ({i.points})" for i in plan.skipped))
    if plan.unresolved_points:
        print(f"Points outside every state/province/country extract (e.g. offshore): {plan.unresolved_points:,}")


def cmd_coverage(a):
    from . import aggregate, match
    from .coverage import load_coverage, make_plan, newly_covered
    from .valhalla import disk_usage, fetch_extracts, merge_extracts, rebuild_tiles, set_extracts

    cfg = load_config()
    con = connect(cfg, read_only=True)
    plan = make_plan(con, cfg, refresh_index=a.refresh_index)
    con.close()
    print_plan(plan)
    if a.action == "plan" or not plan.items:
        return
    old_cov = load_coverage(cfg)
    before = disk_usage()
    set_extracts(plan.current + [it.region.path for it in plan.items])
    cfg = load_config()
    print("\n[1/4] fetch + verify + merge extracts")
    merge_extracts(fetch_extracts(cfg), cfg)
    print("[2/4] rebuild routing tiles (this takes a while)")
    rebuild_tiles(cfg)
    con = connect(cfg)
    ids = newly_covered(con, old_cov, load_coverage(cfg))
    con.close()
    print(f"[3/4] match + fill {len(ids):,} newly covered drives")
    print(f"      {match.run(drive_ids=ids, cfg=cfg)}")
    print("[4/4] aggregate")
    print(f"      {aggregate.run(cfg)}")
    print(f"\ndata/valhalla: {_gb(before)} -> {_gb(disk_usage())}")


def cmd_demo(a):
    from .demo import run

    run(a.format, serve=not a.no_serve, port=a.port)


def cmd_serve(a):
    from .server import serve

    serve(a.host, a.port, a.dev)


def main(argv=None):
    p = argparse.ArgumentParser(prog="drivelog", description="Local history of every road driven.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def dates(sp):
        sp.add_argument("--since", help="YYYY-MM-DD")
        sp.add_argument("--until", help="YYYY-MM-DD")

    sp = sub.add_parser("top", help="most-driven roads")
    sp.add_argument("--n", type=int, default=25)
    sp.add_argument("--by", choices=["way", "road"], default="road")
    sp.add_argument("--observed-only", action="store_true", help="count only GPS-confirmed road use")
    dates(sp)
    sp.set_defaults(fn=cmd_top)

    sp = sub.add_parser("dates", help="dates a road was driven")
    sp.add_argument("road", help='ref number, ref or name: "72", "HI 72", "Kailua Rd"')
    sp.add_argument("--limit", type=int, default=0, help="max dates per road (0 = all)")
    sp.add_argument("--roads", type=int, default=1, help="how many matching roads to show")
    dates(sp)
    sp.set_defaults(fn=cmd_dates)

    sp = sub.add_parser("day", help="drives on a date, with roads in order")
    sp.add_argument("date", help="YYYY-MM-DD")
    sp.set_defaults(fn=cmd_day)

    sp = sub.add_parser("drive", help="one drive: route, new roads, similar drives")
    sp.add_argument("drive", help="drive id / prefix, or 'YYYY-MM-DD HH:MM'")
    sp.set_defaults(fn=cmd_drive)

    sp = sub.add_parser("stats", help="miles and drive count per period")
    sp.add_argument("--by", choices=["year", "month", "week", "day"], default="month")
    dates(sp)
    sp.set_defaults(fn=cmd_stats)

    sub.add_parser("extract", help="extract drives from the Timeline export").set_defaults(fn=cmd_extract)
    sp = sub.add_parser("match", help="map-match drives with Valhalla")
    sp.add_argument("--rematch", action="store_true", help="rematch drives already matched")
    sp.set_defaults(fn=cmd_match)
    sub.add_parser("validate", help="run the match validation set").set_defaults(fn=cmd_validate)
    sp = sub.add_parser("fill", help="(re)route the gaps between observed segments")
    sp.add_argument("--refill", action="store_true", help="redo fills for all matched drives")
    sp.set_defaults(fn=cmd_fill)
    sub.add_parser("aggregate", help="rebuild road statistics and GeoJSON").set_defaults(fn=cmd_aggregate)
    sub.add_parser("rebuild", help="extract + match new drives + aggregate").set_defaults(fn=cmd_rebuild)

    sp = sub.add_parser("valhalla", help="Valhalla helpers")
    sp.add_argument("action", choices=["fetch", "compose", "configure", "health"])
    sp.set_defaults(fn=cmd_valhalla)

    sp = sub.add_parser("area", help="driving in a zip code, town, county, state or country")
    sp.add_argument("query", help='"96734", "Kailua", "Honolulu County", "Kailua, Honolulu County"')
    sp.add_argument("--limit", type=int, default=30, help="max dates to list (0 = all)")
    sp.set_defaults(fn=cmd_area)
    sp = sub.add_parser("areas", help="area boundaries")
    sp.add_argument("action", choices=["build"])
    sp.set_defaults(fn=cmd_areas)

    sp = sub.add_parser("patterns", help="when you drive: hour × weekday, per month, drive lengths")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--road", help='only drives on this road ("72")')
    g.add_argument("--area", help='only drives in this area ("96734", "Kailua")')
    sp.add_argument("--miles", action="store_true", help="show miles instead of drive counts")
    sp.set_defaults(fn=cmd_patterns)
    sp = sub.add_parser("review", help="year in review")
    sp.add_argument("year", help="a year, or 'all'")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("coverage", help="plan or add Geofabrik extracts for drives outside coverage")
    sp.add_argument("action", choices=["plan", "apply"])
    sp.add_argument("--refresh-index", action="store_true", help="re-download Geofabrik's region index")
    sp.set_defaults(fn=cmd_coverage)

    sp = sub.add_parser("demo", help="try it on synthetic data (Oahu): builds everything under data/demo/")
    sp.add_argument("--format", choices=["ios", "android"], default="ios", help="which synthetic export to load")
    sp.add_argument("--no-serve", action="store_true", help="build the demo database but don't open the viewer")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_demo)

    sp = sub.add_parser("serve", help="run the local viewer")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--dev", action="store_true", help="also run the Vite dev server with hot reload")
    sp.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    load_config()  # fail early on a bad config
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
