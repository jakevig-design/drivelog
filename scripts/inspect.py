"""Phase 0: profile a Google Timeline export without assuming its schema.

Streams the file with ijson and writes data/reports/data_profile.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This file is named inspect.py; stop it shadowing the stdlib `inspect` module.
sys.path = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]

import bisect
import collections
import json
import statistics
from datetime import datetime, timedelta

import ijson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drivelog.config import load_config  # noqa: E402
from drivelog.geo import haversine_m, parse_coord  # noqa: E402

M_PER_MI = 1609.344
SEGMENT_KINDS = ("visit", "activity", "timelinePath", "timelineMemory")


def detect_format(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(4096).lstrip()
    if head.startswith(b"["):
        return "ios"
    if head.startswith(b"{") and b"semanticSegments" in head[:4096]:
        return "android"
    if head.startswith(b"{"):
        # Android files may put other keys first; check by streaming keys.
        with path.open("rb") as f:
            for prefix, event, value in ijson.parse(f):
                if prefix == "" and event == "map_key" and value == "semanticSegments":
                    return "android"
    return "unknown"


def iter_segments(path: Path, fmt: str):
    prefix = "item" if fmt == "ios" else "semanticSegments.item"
    with path.open("rb") as f:
        yield from ijson.items(f, prefix, use_float=True)


def count_raw_signals(path: Path, fmt: str) -> int:
    if fmt != "android":
        return 0
    n = 0
    with path.open("rb") as f:
        for _ in ijson.items(f, "rawSignals.item"):
            n += 1
    return n


def seg_kind(seg: dict) -> str:
    for k in SEGMENT_KINDS:
        if k in seg:
            return k
    return "other"


def round_coords(obj):
    """Round every coordinate string in a record to 2 decimals for the report."""
    if isinstance(obj, dict):
        return {k: round_coords(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_coords(v) for v in obj]
    if isinstance(obj, str):
        c = parse_coord(obj)
        if c is not None:
            return f"geo:{c[0]:.2f},{c[1]:.2f}"
    return obj


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def fmt_num(v, nd=0):
    return "–" if v is None else f"{v:,.{nd}f}"


def main():
    cfg = load_config()
    path = cfg.timeline_path
    fmt = detect_format(path)
    drive_types = {t.lower() for t in cfg.drive_types}

    counts = collections.Counter()
    top_keys = collections.Counter()
    type_count = collections.Counter()
    type_dist = collections.Counter()
    samples = collections.defaultdict(list)
    months = set()
    min_t = max_t = None
    drives = []  # (start, end)
    path_points = []  # (ts, lat, lng)

    for seg in iter_segments(path, fmt):
        kind = seg_kind(seg)
        counts[kind] += 1
        top_keys[tuple(sorted(seg.keys()))] += 1
        if len(samples[kind]) < 3:
            samples[kind].append(round_coords(seg))
        start = datetime.fromisoformat(seg["startTime"])
        end = datetime.fromisoformat(seg["endTime"])
        min_t = start if min_t is None or start < min_t else min_t
        max_t = end if max_t is None or end > max_t else max_t
        months.add((start.year, start.month))

        if kind == "activity":
            act = seg["activity"]
            t = (act.get("topCandidate") or {}).get("type", "?")
            type_count[t] += 1
            type_dist[t] += float(act.get("distanceMeters") or 0)
            if t.lower() in drive_types:
                drives.append((start, end))
        elif kind == "timelinePath":
            for p in seg["timelinePath"]:
                c = parse_coord(p.get("point", ""))
                if c is None:
                    continue
                off = float(p.get("durationMinutesOffsetFromStartTime") or 0)
                path_points.append((start + timedelta(minutes=off), c[0], c[1]))

    raw_signals = count_raw_signals(path, fmt)

    # Point density inside driving windows, by year.
    path_points.sort(key=lambda p: p[0])
    pts_ts = [p[0] for p in path_points]
    dens_s = collections.defaultdict(list)
    dens_m = collections.defaultdict(list)
    pts_per_drive = collections.defaultdict(list)
    for start, end in drives:
        i = bisect.bisect_left(pts_ts, start)
        j = bisect.bisect_right(pts_ts, end)
        pts = path_points[i:j]
        pts_per_drive[start.year].append(len(pts))
        for a, b in zip(pts, pts[1:]):
            dens_s[start.year].append((b[0] - a[0]).total_seconds())
            dens_m[start.year].append(haversine_m(a[1], a[2], b[1], b[2]))

    # Months with no data.
    missing = []
    y, m = min_t.year, min_t.month
    while (y, m) <= (max_t.year, max_t.month):
        if (y, m) not in months:
            missing.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1

    drive_type_found = [t for t in type_count if t.lower() in drive_types]

    out = []
    w = out.append
    w("# Timeline data profile\n")
    w(f"- **Source:** `{path.name}` ({path.stat().st_size / 1e6:.1f} MB)")
    w(f"- **Detected format:** {fmt}")
    if fmt == "ios":
        w("- **Top-level structure:** JSON array of segments; each has `startTime`, `endTime` "
          "and exactly one of `visit`, `activity`, `timelinePath`, `timelineMemory`.")
        w("- Coordinates are strings `geo:LAT,LNG`. Path points carry "
          "`durationMinutesOffsetFromStartTime` (whole minutes) relative to the path segment start.")
        w("- `timelinePath` segments are separate from activities (fixed windows, UTC `Z` "
          "timestamps); activity/visit timestamps carry a local UTC offset.")
    w(f"- **Date range:** {min_t.isoformat()} → {max_t.isoformat()}")
    w(f"- **Driving activity type string:** {', '.join(f'`{t}`' for t in drive_type_found) or 'NOT FOUND'}")
    w("")
    w("## Segment counts\n")
    w("| Kind | Count |\n|---|---:|")
    for k, v in counts.most_common():
        w(f"| {k} | {v:,} |")
    w(f"| raw signals | {raw_signals:,} |")
    w(f"| path points (total) | {len(path_points):,} |")
    w("\nKey sets seen:\n")
    for k, v in top_keys.most_common():
        w(f"- `{', '.join(k)}`: {v:,}")
    w("\n## Activity types\n")
    w("| Type | Count | Total mi |\n|---|---:|---:|")
    for t, n in type_count.most_common():
        w(f"| {t} | {n:,} | {type_dist[t] / M_PER_MI:,.0f} |")
    w("\n## Driving point density by year\n")
    w("Path points that fall inside driving activity windows.\n")
    w("| Year | Drives | Median pts/drive | Median s between | p90 s | Median mi between | p90 mi |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for yr in sorted(pts_per_drive):
        s, mm = dens_s[yr], dens_m[yr]
        w(f"| {yr} | {len(pts_per_drive[yr]):,} | {fmt_num(statistics.median(pts_per_drive[yr]))} | "
          f"{fmt_num(statistics.median(s) if s else None)} | {fmt_num(pct(s, 0.9))} | "
          f"{fmt_num(statistics.median(mm) / M_PER_MI if mm else None, 2)} | {fmt_num(pct(mm, 0.9) / M_PER_MI if mm else None, 2)} |")
    w("\n## Months with no data\n")
    w(", ".join(missing) if missing else "None.")
    w("\n## Sample records (coordinates rounded to 2 decimals)\n")
    for k in SEGMENT_KINDS:
        if samples.get(k):
            w(f"### {k}\n")
            for s in samples[k]:
                txt = json.dumps(s, indent=2)
                if len(txt) > 1500:
                    txt = txt[:1500] + "\n  … (truncated)"
                w(f"```json\n{txt}\n```")

    report = ROOT / "data/reports/data_profile.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(out) + "\n")
    print(f"format={fmt} segments={sum(counts.values()):,} drive_types={drive_type_found} "
          f"driving_mi={sum(type_dist[t] for t in drive_type_found) / M_PER_MI:,.0f}")
    print(f"wrote {report.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
