"""Phase 3 validation: match a fixed, stratified set of 20 drives and report quality."""

from __future__ import annotations

import copy
import json
import random
import statistics
from datetime import timezone

from .config import ROOT, Config, load_config
from .coverage import load_coverage
from .db import connect
from .geo import path_length_m
from .extract import moving_window
from .fill import fill_drive, finalize, load_habit, load_pace
from .match import Matcher

REPORT = ROOT / "data/reports/match_validation.md"
MI = 1.609344  # km per mile (report output is in miles and feet)
FT = 3.28084


def pick_validation_set(con, coverage, n=20, seed=7) -> list[str]:
    """Spread across years, and within each year take the sparsest and densest in-coverage drives
    (points per km), plus random fill."""
    rows = con.execute("""
        SELECT d.drive_id, year(d.local_date) AS y, d.reported_distance_m, d.n_points,
               avg(p.lat) AS lat, avg(p.lng) AS lng
        FROM drives d JOIN drive_points p USING (drive_id)
        WHERE d.reported_distance_m >= 2000
        GROUP BY ALL""").fetchall()
    rows = [r for r in rows if coverage.contains(r[4], r[5])]
    by_year: dict[int, list] = {}
    for r in rows:
        by_year.setdefault(r[1], []).append(r)
    picked: list[str] = []
    for y in sorted(by_year):
        ys = sorted(by_year[y], key=lambda r: r[3] / (r[2] / 1000))
        for r in (ys[0], ys[len(ys) // 2], ys[-1]):
            if r[0] not in picked:
                picked.append(r[0])
    rng = random.Random(seed)
    rng.shuffle(picked)
    picked = picked[:n]
    rest = [r[0] for r in rows if r[0] not in picked]
    rng.shuffle(rest)
    picked += rest[: n - len(picked)]
    return sorted(picked)


def evaluate(cfg: Config, drive_ids: list[str], overrides: dict | None = None) -> list[dict]:
    cfg = copy.deepcopy(cfg)
    cfg.raw.setdefault("valhalla", {}).update(overrides or {})
    con = connect(cfg, read_only=True)
    matcher = Matcher(cfg, load_coverage(cfg))
    habit = load_habit(con)
    pace = load_pace(con)
    out = []
    for did in drive_ids:
        start, rep, npts = con.execute(
            "SELECT local_start, reported_distance_m, n_points FROM drives WHERE drive_id = ?", [did]).fetchone()
        pts = [(ts.replace(tzinfo=timezone.utc), lat, lng) for ts, lat, lng in con.execute(
            "SELECT ts, lat, lng FROM drive_points WHERE drive_id = ? ORDER BY seq", [did]).fetchall()]
        r = matcher.match_drive(did, rep, pts)
        window = moving_window(pts, cfg.stationary_m)
        fills = fill_drive(matcher, habit, [(0, g) for g in r.gaps], window, pace) if r.status == "unfilled" else []
        filled_m = sum(f.route_m for _, f in fills if f.edges)
        low_m = sum(f.route_m for _, f in fills if f.edges and f.confidence == "low")
        if r.status == "unfilled":
            r.status, r.score, r.detail = finalize(r.detail, r.matched_length_m, filled_m, low_m, rep)
            r.matched_length_m += filled_m
        path_m = path_length_m([(p[1], p[2]) for p in pts])
        out.append({
            "path_km": path_m / 1000,
            "path_ratio": round(r.matched_length_m / path_m, 3) if path_m else None,
            # Reported distance disagrees with the drive's own points (see DEBUG.md, Phase 3).
            "unreliable": path_m > 1.5 * rep,
            "drive_id": did, "start": start, "reported_km": rep / 1000, "points": npts,
            "pts_per_km": npts / (rep / 1000), "status": r.status,
            "matched_km": r.matched_length_m / 1000,
            "ratio": r.detail.get("length_ratio"), "share": r.detail.get("point_share"),
            "mean_dist": r.detail.get("mean_dist_m"), "split": r.detail.get("split_used"),
            "errors": r.detail.get("errors"),
            "filled_km": filled_m / 1000, "low_km": low_m / 1000,
            "gaps": len(fills), "gaps_high": sum(1 for _, f in fills if f.edges and f.confidence == "high"),
        })
    con.close()
    return out


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] != "failed"]
    ratios = [r["ratio"] for r in ok if r["ratio"] is not None and not r["unreliable"]]
    all_ratios = [r["ratio"] for r in ok if r["ratio"] is not None]
    fails = sum(1 for r in results if r["status"] == "failed")
    return {
        "median_ratio": statistics.median(ratios) if ratios else None,
        "median_ratio_all": statistics.median(all_ratios) if all_ratios else None,
        "median_path_ratio": statistics.median([r["path_ratio"] for r in ok if r["path_ratio"]]) if ok else None,
        "unreliable": sum(r["unreliable"] for r in results),
        "failure_rate": fails / len(results),
        "median_share": statistics.median([r["share"] or 0 for r in results]),
        "ok": sum(r["status"] == "ok" for r in results),
        "partial": sum(r["status"] == "partial" for r in results),
    }


def write_report(results, summary, params, sweep=None, notes: str = "") -> None:
    lines = ["# Map-matching validation\n",
             f"Parameters: `{json.dumps(params)}`\n",
             f"- Median matched/reported length ratio: **{summary['median_ratio']:.3f}** (target 0.90–1.15), "
             f"over the {20 - summary['unreliable']} drives whose reported distance agrees with their own points",
             f"- Same ratio over all drives, including the {summary['unreliable']} with unreliable reported distance: "
             f"{summary['median_ratio_all']:.3f}",
             f"- Median matched length / point-path length: {summary['median_path_ratio']:.3f} "
             "(the point path is a straight-line lower bound, so ≥ 1 is expected)",
             f"- Failure rate: **{summary['failure_rate']:.0%}** (target < 5%)",
             f"- Median share of points matched: {summary['median_share']:.2f}",
             f"- ok: {summary['ok']}, partial: {summary['partial']}\n"]
    lines.append("⚠ = reported distance disagrees with the drive's own points (point path > 1.5 × reported).\n")
    if notes:
        lines += [notes, ""]
    if sweep:
        lines += ["## Parameter sweep\n",
                  "| Params | Median ratio (reliable) | Median ratio (all) | Path ratio | Failure rate | Median share | ok | partial |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for p, s in sweep:
            lines.append(f"| `{json.dumps(p)}` | {s['median_ratio']:.3f} | {s['median_ratio_all']:.3f} | "
                         f"{s['median_path_ratio']:.3f} | {s['failure_rate']:.0%} | "
                         f"{s['median_share']:.2f} | {s['ok']} | {s['partial']} |")
        lines.append("")
    lines += ["## Drives\n",
              "| Drive | Start (local) | Points | Pts/mi | Reported mi | Point-path mi | Matched mi | Ratio | Path ratio | Pts matched | Mean dist ft | Status |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in results:
        lines.append(
            f"| `{r['drive_id'][:8]}` | {r['start']:%Y-%m-%d %H:%M} | {r['points']} | {r['pts_per_km'] * MI:.2f} | "
            f"{r['reported_km'] / MI:.1f}{' ⚠' if r['unreliable'] else ''} | {r['path_km'] / MI:.1f} | {r['matched_km'] / MI:.1f} | "
            f"{r['ratio'] if r['ratio'] is not None else '–'} | {r['path_ratio'] if r['path_ratio'] is not None else '–'} | "
            f"{r['share'] if r['share'] is not None else '–'} | {round(r['mean_dist'] * FT) if r['mean_dist'] is not None else '–'} | "
            f"{r['status']}{' (split)' if r['split'] else ''} |")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")


def run(sweep_params: list[dict] | None = None, notes: str = "") -> dict:
    cfg = load_config()
    con = connect(cfg, read_only=True)
    ids = pick_validation_set(con, load_coverage(cfg))
    con.close()
    sweep = []
    for p in sweep_params or []:
        s = summarize(evaluate(cfg, ids, p))
        sweep.append((p, s))
        print(p, s, flush=True)
    results = evaluate(cfg, ids)
    summary = summarize(results)
    params = {k: v for k, v in cfg.valhalla.items() if k not in ("url", "extracts", "workers")}
    write_report(results, summary, params, sweep, notes)
    print("final", params, summary)
    return summary
