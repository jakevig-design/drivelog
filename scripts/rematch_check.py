"""Dry-run match + fill for a fixed sample of drives, without writing to the database.

    uv run python scripts/rematch_check.py OUT.json [--n 20] [--ids id1,id2,...]

Writes each drive's observed edges, gaps and fills (way ids, rounded lengths, confidence) to OUT.json, so
two runs (e.g. before and after a Valhalla change) can be compared with `diff`. The sample is the first N
drives by drive_id among matched drives, so it's the same on every run.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/inspect.py shadows the stdlib module when scripts/ is on sys.path (DEBUG.md, Phase 0).
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p and str(Path(p).resolve()) != _here]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import timezone  # noqa: E402

from drivelog.config import load_config  # noqa: E402
from drivelog.coverage import load_coverage  # noqa: E402
from drivelog.db import connect  # noqa: E402
from drivelog.fill import fill_gap, load_habit, load_pace  # noqa: E402
from drivelog.match import Edge, Matcher, load_points  # noqa: E402


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--ids", help="comma-separated drive ids instead of the default sample")
    a = ap.parse_args()
    cfg = load_config()
    con = connect(cfg, read_only=True)
    if a.ids:
        ids = a.ids.split(",")
    else:
        ids = [r[0] for r in con.execute(
            "SELECT drive_id FROM drives WHERE match_status IN ('ok','partial') ORDER BY drive_id LIMIT ?",
            [a.n]).fetchall()]
    pts = load_points(con, ids)
    info = {r[0]: r[1:] for r in con.execute(
        "SELECT drive_id, reported_distance_m, moving_start, moving_end FROM drives WHERE drive_id IN (SELECT unnest(?))",
        [ids]).fetchall()}
    habit, pace = load_habit(con), load_pace(con)
    con.close()
    m = Matcher(cfg, load_coverage(cfg))
    out = {}
    for d in ids:
        r = m.match_drive(d, info[d][0], pts[d])
        items = []
        for it in r.items:
            if isinstance(it, Edge):
                items.append(["edge", it.way_id, round(it.length_m, 1)])
            else:
                f = fill_gap(m, habit, it, (_utc(info[d][1]), _utc(info[d][2])), pace)
                items.append(["gap", round(it.straight_m, 1), f.confidence if f.edges else None,
                              [[e.way_id, round(e.length_m, 1)] for e in f.edges]])
        out[d] = {"status": r.status, "observed_m": round(r.matched_length_m or 0, 1), "items": items}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    n_edges = sum(1 for v in out.values() for i in v["items"] if i[0] == "edge")
    n_gaps = sum(1 for v in out.values() for i in v["items"] if i[0] == "gap")
    print(f"{len(out)} drives, {n_edges} observed edges, {n_gaps} gaps -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
