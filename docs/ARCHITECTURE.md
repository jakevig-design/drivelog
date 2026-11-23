# Architecture

Drivelog turns a Google Timeline export into a history of every road you've driven: which roads, how
often, on which days, and when. Everything runs on your machine: a local routing engine (Valhalla in
Docker), a local database (DuckDB), and a local web viewer (FastAPI + React + MapLibre).

## Pipeline

```
Timeline export ──extract──▶ drives + points ──split──▶ ──match──▶ observed road segments + gaps
                                                 │
                                                 └──fill──▶ inferred segments (with confidence)
                                                               │
                                     aggregate ◀───────────────┘
                                         │
               roads, road keys, traversals, first drives, per-road dates
                                         │
                    areas (zip / place / county / state / country) ── patterns ── year in review
                                         │
                                  CLI and viewer
```

| Stage | Module | What it does |
|---|---|---|
| extract | `drivelog/extract.py` | Reads the export with a per-format adapter (iPhone or Android), keeps driving activities, attaches the path points inside each activity window, drops impossible points (speed outliers), and computes each drive's *moving window*. Re-imports are deduplicated by time window. |
| split | `drivelog/split.py` | Finds activities Google stitched across a stop (10+ minutes standing still, seen in the points or hidden in a gap) and replaces each with its separate drives. |
| match | `drivelog/match.py` | Map-matches each drive's points with Valhalla `/trace_attributes`. Stores only what the points support (**observed** edges) and records the **gaps** between observed stretches. |
| fill | `drivelog/fill.py` | Routes each gap with Valhalla `/route` (fastest plus alternates), picks the candidate that best fits the time and your habits, and stores it as **inferred** edges with a confidence label. |
| aggregate | `drivelog/aggregate.py` | Groups OSM ways into named roads ("road keys"), builds per-traversal, per-road and per-day tables, and records the first drive on every road and way. |
| areas | `drivelog/areas.py` | Loads zip code (Census ZCTA) and administrative boundaries (from the same OSM extracts) and records how much of each driven way lies in each area. |
| patterns | `drivelog/patterns.py` | Hour × weekday grids, monthly series, length distributions, year in review. |
| viewer | `drivelog/server.py`, `web/` | Local-only HTTP API and map UI. |

## Why two stages: match, then fill

Timeline exports are sparse. Google thins the stored track, so consecutive points of a drive can be
minutes and kilometres apart. A map matcher asked to bridge a huge gap either gives up (the stretch is
lost) or searches a vast candidate space (slow, and memory-hungry enough to crash the routing server).

So matching is split:

1. **Match** splits each trace wherever consecutive points are more than `breakage_distance` apart,
   matches the pieces, and keeps only edges the points actually support. Between observed stretches
   it records a gap with the trace timestamps on both sides (when known).
2. **Fill** routes each gap. Because it runs after every drive is matched, it can use a prior that
   only exists then: the set of roads you've been *observed* on. Refilling is cheap and needs no
   rematch, so the confidence model can be tuned and re-applied in seconds.

## Choosing a route for a gap

For each gap, Valhalla returns the fastest route plus up to two alternates. Each candidate gets:

```
score = |ln(elapsed / (route_time × pace))| − 0.7 × habit_share
```

- `elapsed` is the time that actually passed between the two points, clamped to the drive's moving
  window (below).
- `route_time` is Valhalla's estimate for that candidate, scaled by your **pace**: the median
  elapsed/route-time ratio over previous fills. Routing engines assume conservative speeds; most
  drivers are faster, and without this calibration ordinary drives on familiar roads look
  "too fast" and get flagged.
- `habit_share` is the share of the candidate's length on roads you've been observed on.

The lowest score wins. Without timing, the fastest route wins, bent by familiarity.

## Confidence model

Every road use is labelled with how it's known:

| Label | Meaning |
|---|---|
| observed | GPS points put the drive on this road. |
| `~` inferred, high | Routed across a gap, and the route explains the gap: the gap is short (< 1.5 km); or the time fits (elapsed within 0.7–1.6× the pace-scaled route time); or the route is ≥ 80% on your observed roads and the time is at least plausible (0.5–2.5×). |
| `?` inferred, low | Routed across a gap, but nothing above holds: an unfamiliar route whose time doesn't fit, a gap without timestamps, or a route that would need an average above 90 mph (broken timestamps). |

Timeline path times are rounded to the minute, so each band test allows ±60 s.

A drive is `partial` when more than half of it is inferred, more than a quarter is low confidence, part
of it lies outside map coverage, or its matched length is far from the reported distance.

## The moving window

A Timeline driving activity often starts before the car moves (the phone notices the trip while you're
still in the driveway) and ends after it stops. Those parked minutes make the first and last gaps look
slow, which would push their routes to low confidence.

Extraction trims them: leading points within `[fill] stationary_m` (default 100 m) of the first point
collapse to the last of them (same place, the latest time the car was known to be there), and likewise
for the tail. The resulting `moving_start` / `moving_end` time the fills; the original activity times
are kept for display.

## Stitched activities

Timeline sometimes records several drives, or a whole day, as one driving activity. `drivelog split`
cuts an activity wherever the car stopped for 10 minutes or more (after allowing a minute for
timestamp rounding). There are two kinds of stop:

- **observed**: consecutive points staying within `stationary_m` of the first of them;
- **hidden**: two consecutive points where the time between them exceeds the time to drive between
  them (Valhalla's route time × pace × 1.6, the top of the time-fit band) by 10+ minutes.

The threshold came from the data: stops inside drives fall away after a few minutes (traffic lights),
and from 10 minutes up nearly all of them sit at places where other drives start or end. Where a hidden
stop happened along its gap isn't known; the rule is that the car waited at the earlier point and then
drove at its usual pace, so the next drive starts there at an estimated time, and fill treats that
first gap as untimed. A stop at the very start or end trims the drive instead of splitting it. Pieces
shorter than `min_distance_m` are dropped. Each piece records the activity it came from
(`drives.split_from`), and a re-import treats the stitched activity as a duplicate of its pieces.

## Road keys

A numbered route is one road however OSM fragments or spells it: "Route 72", "State Route 72" and
"HI-72" all normalise to `HI 72`, with unqualified state-route numbers resolved by co-occurrence on the
same way. Concurrencies count for each route. Unnumbered streets are grouped by name into spatially
connected pieces, so two different "Oak Street"s in different towns stay separate roads. The full rules
are in the `drivelog/aggregate.py` docstring.

## Areas

- **Zip codes** are Census ZCTAs (2020, 1:500k). ZCTAs approximate USPS zip code areas; they aren't
  identical.
- **Places, counties, states, countries** are OSM boundaries (`boundary=administrative`, plus
  census-designated places) taken from the routing extracts. Admin levels mean different things in
  different countries, so a small per-country table maps them to a common kind (locality, county,
  state/province, country). A country whose own boundary is cut off by a regional extract is built as
  the union of its states.
- Each driven way records the share of its length inside each area, so a way that crosses a boundary
  counts toward both sides by length.
- On the map, an area's roads are cut to its (simplified) outline, so nothing is drawn past the
  boundary. That clipping is display-only: miles use the length shares above.

## Coverage

Routing needs OSM extracts covering where you drove. `drivelog coverage plan` loads Geofabrik's region
index, finds every drive point outside the current extracts, and picks, for each point, the smallest
state/province or country extract containing it (never a continent or the whole US). `drivelog
coverage apply` adds them to the config, downloads and checksum-verifies them, merges them into one
deduplicated PBF (separately loaded overlapping extracts break Valhalla's tile build), rebuilds the
tiles (keeping the previous build to restore on failure), and matches only the newly covered drives.

## Time

Drives are stored in UTC with the local wall time taken from each timestamp's own UTC offset, so a
trip in another time zone shows its local hours. A timestamp without an offset uses `fallback_tz`.

## Privacy

- Nothing leaves the machine. Routing, geocoding and boundary lookups are all local. The network is
  used only to download OSM extracts (Geofabrik), Census boundary files, packages and the Docker image,
  and for keyless basemap tiles in the browser.
- The routing server and the viewer bind to `127.0.0.1` only.
- Your export, database and everything derived from it live under paths that are gitignored.

## Design choices

- **DuckDB** for storage: one file, fast columnar aggregation for the per-road and per-day tables, and
  a spatial extension for the boundary work, with no server.
- **Valhalla in Docker** rather than a public routing API: the only way to map-match a location history
  without sending it anywhere.
- **The viewer works from one traversal feed in the browser.** The server sends every (way, day,
  drive) traversal once. The frequency map's year range, its drives/miles toggle, and the
  explored-over-time animation (first month per way, running distinct miles) are all recomputed locally
  and pushed to MapLibre feature state, with no refetch.
- **Basemaps** are keyless tiles: OpenFreeMap (street map, light and dark) and Esri World Imagery
  (satellite). Overlay colours follow the ground: the frequency ramp runs light to dark on the street
  map and flips on dark or satellite ground so busy roads stay brightest.
- **One map layer model.** Everything drawn (a day, a drive, an area's roads, a year's new roads) is a
  layer in its own colour, listed in "On the map"; routes sharing a road are offset side by side so
  neither hides the other.
