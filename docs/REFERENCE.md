# Reference

The [README](../README.md) is the step-by-step guide. This page has every command, the limits worth
knowing, and development notes.

## Commands

All run from the Drivelog folder as `uv run drivelog …`. Every command has `--help`.

**Everyday**

```sh
uv run drivelog start [--demo] [--port 8765] [--no-open]  # read the export, open the dashboard
uv run drivelog serve                   # open the viewer on what's already loaded
uv run drivelog rebuild                 # after a new export: extract, split, match + fill new drives, aggregate
```

**Questions from the terminal** (the examples use the demo data)

```sh
uv run drivelog top --n 25 [--since 2016-01-01] [--until …] [--observed-only] [--by way]
uv run drivelog dates "72" [--limit 20] [--roads 3]   # every date you drove it; ~ / ? mark inferred drives
uv run drivelog day 2016-03-12          # each drive's roads, in order; *new* = first time on a road
uv run drivelog drive "2016-03-12 11:35"  # one drive: route, new roads, similar drives (or an id prefix)
uv run drivelog area 96734              # also "Kailua", "Honolulu County", "Kailua, Honolulu County"
uv run drivelog patterns [--road 72 | --area 96734] [--miles]
uv run drivelog review 2015             # or: review all
uv run drivelog stats --by month        # or year, week, day; --since / --until
```

`drivelog top` counts a road once however OSM splits it; `--by way` lists the individual OSM ways.

**Road detail setup and pipeline stages**

```sh
uv run drivelog coverage plan           # which Geofabrik extracts cover your drives, sizes, disk estimate
uv run drivelog coverage apply          # add them, download + verify, build routing tiles, match covered drives
uv run drivelog extract                 # read the export into data/drivelog.duckdb
uv run drivelog split [--dry-run]       # split activities Google stitched across a stop (needs the routing engine)
uv run drivelog match [--rematch]       # match + fill every drive (resumable)
uv run drivelog fill --refill           # re-choose the routes across gaps (no rematch needed)
uv run drivelog aggregate               # road statistics
uv run drivelog areas build             # zip codes and place boundaries from the map data
uv run drivelog validate                # match a fixed sample and write a quality report to data/reports/
uv run drivelog valhalla health         # test-match one of your drives against the routing engine
```

By hand instead of `coverage apply`: list extracts in `config.toml`, then `drivelog valhalla fetch`
(download, checksum, merge), `drivelog valhalla compose`, `docker compose up -d` (the first start builds
the routing tiles), `drivelog valhalla configure && docker compose restart`.

**The demo with road detail**: `uv run drivelog demo` builds the Oahu demo with its own routing container
(port 8003) under `data/demo/`, without touching your own setup. `--format android` loads the
Android-format export instead.

## Dashboard links

The dashboard keeps its filters in the address, so any view can be bookmarked:
`http://127.0.0.1:8765/?year=2024&dow=5,6`, `?town=…&len=l20p`, and for compare
`?dow=0,1,2,3,4&compare=1&b_dow=5,6` (`b_` marks the second set). Map views open from links like `#day=2016-03-12`,
`#road=HI%2072`, `#area=zip:96734`, `#year=2016` and `#explore=2016-01`; add `&sat` for satellite.

## Limits

- **Quick mode** (before road detail): miles follow the straight line between GPS points (≈, a little low).
  Places come from Census boundaries in the US and Natural Earth provinces and countries elsewhere, so
  towns outside the US come with road detail.
- **Sparse points.** Timeline exports thin the GPS track, so points can be minutes apart. Between them the
  route is **inferred**: `~` means the route fits the time or your usual roads; `?` means low confidence.
  Roads known only from inferred stretches draw faded on the map, and `drivelog top --observed-only` counts
  GPS-confirmed use only.
- **Coverage.** Drives outside your map extracts are listed as out of coverage until you add their region
  (`drivelog coverage plan`).
- **Stitched drives.** Timeline sometimes records several drives (or a whole day) as one activity. Drivelog
  splits an activity wherever the car stood still for 10+ minutes, whether seen in the points or hidden in a
  gap too long for the drive it covers.
- **Miles** are the length of the matched route. Google's own reported distance is often far too low, so
  it's used only for drives that couldn't be matched.
- **Zip codes** are Census ZCTAs, which approximate USPS zip codes. They cover the US only: elsewhere, filter
  by town, county, state/province or country.
- **Places** come from OpenStreetMap boundaries once road detail is set up; a place OSM doesn't map as a
  boundary can't be filtered by name (use its zip code).
- **Same-named streets** are told apart by town ("Bramblewick Street · Wexbury, AA"). In neighbouring towns they may
  occasionally split into two roads, or two roads join, where the drives never connect them.

## Development

```sh
uv run pytest                           # unit tests on synthetic data; routing calls are mocked
uv run drivelog serve --dev             # also runs the Vite dev server with hot reload (port 5173; needs Node)
uv run python scripts/inspect.py        # profile an export's structure into data/reports/
uv run python scripts/make_demo_data.py # regenerate the demo exports (needs the demo's routing server)
uv run --with playwright --with pillow python scripts/screens.py OUT VIEWS.json  # screenshots + network audit
```

The built viewer (`web/dist`) is committed so that running Drivelog doesn't need Node. After changing
`web/src`, `npm run build` in `web/` (or `drivelog serve`, which rebuilds when Node is installed).
CI runs the tests and builds the viewer on every push.
