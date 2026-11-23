import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import {
  api,
  fmtDate,
  fmtMi,
  type Confidence,
  type DayDrive,
  type DriveDetail,
  type Meta,
  type Road,
  type RoadSeg,
  type SearchHit,
  type Traversals,
  type Area,
  type AreaHit,
  type SearchResult,
} from "./api";
import Patterns, { type Scope } from "./Patterns";
import Year from "./Year";

type MLMap = maplibregl.Map;
type Mode = "roads" | "explore" | "day" | "drive" | "area" | "patterns" | "year";

/** Something drawn on the map in one colour: a whole day, a single drive, an area's roads, a year's new roads. */
type MapLayer = {
  key: string; // "day:2026-09-18" | "drive:<id>" | "area:<area_id>" | "areaday:<area_id>:<date>" | "new:<year>"
  kind: "day" | "drive" | "area" | "newroads";
  label: string;
  sub: string;
  color: string;
  driveIds: string[];
  fc: GeoJSON.FeatureCollection;
};

const STYLES = {
  light: "https://tiles.openfreemap.org/styles/liberty",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
type Theme = keyof typeof STYLES;
type Basemap = "map" | "satellite";

// Satellite: Esri World Imagery, keyless raster tiles (terms in DECISIONS.md, v3 Phase 4). Only tile
// coordinates go to the server; our own layers are lines, so the style needs no glyphs or sprites.
const SATELLITE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    imagery: {
      type: "raster",
      tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
      tileSize: 256,
      maxzoom: 19,
      attribution: "Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
  },
  layers: [{ id: "imagery", type: "raster", source: "imagery" }],
};
const styleFor = (theme: Theme, basemap: Basemap) => (basemap === "satellite" ? SATELLITE : STYLES[theme]);
/** Map overlays are drawn for a light or a dark ground: imagery is dark. */
const groundOf = (theme: Theme, basemap: Basemap): Theme => (basemap === "satellite" ? "dark" : theme);

// Frequency map: one hue (orange, clear of the blue water and of the first layer colour), light to dark
// on a light ground; on a dark ground the order flips so the busiest roads are the brightest.
const RAMP = {
  light: ["#ef8a4c", "#df6118", "#c2410c", "#963009", "#661f05"], // starts darker than the basemap's pale road fills
  dark: ["#8a3405", "#c2500a", "#ee7424", "#fca15e", "#fed3a8"],
};
type Freq = "drives" | "mi" | "off";

// Each thing put on the map gets the next unused colour.
const COLORS = ["#2563eb", "#f97316", "#16a34a", "#9333ea", "#dc2626", "#0891b2", "#db2777", "#a16207"];
// Routes that share a road are drawn side by side (px offsets per layer), so neither hides the other.
const OFFSETS = [0, 5, -5, 10, -10, 15, -15, 20];
const OUTLINE = { light: "#475569", dark: "#cbd5e1" }; // the searched road, drawn subtly under routes
const NEW_COLOR = "#84cc16";
const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
const PAD = { top: 90, bottom: 60, left: 340, right: 440 };

type Pt = { x: number; y: number };

/** Distance in pixels from p to segment ab. */
function segDist(p: Pt, a: Pt, b: Pt): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) : 0;
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function stored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    if (v && (allowed as readonly string[]).includes(v)) return v as T;
  } catch {
    /* storage unavailable */
  }
  return fallback;
}

/** Hash deep links: "#day=2016-03-05", "#explore=2016-03&sat", "#area=zip:96734&map". */
function hashParams(): Map<string, string> {
  const m = new Map<string, string>();
  for (const part of decodeURIComponent(window.location.hash.slice(1)).split("&")) {
    if (!part) continue;
    const i = part.indexOf("=");
    m.set(i < 0 ? part : part.slice(0, i), i < 0 ? "" : part.slice(i + 1));
  }
  return m;
}

function initialTheme(): Theme {
  try {
    const t = localStorage.getItem("drivelog-theme");
    if (t === "light" || t === "dark") return t;
  } catch {
    /* storage unavailable */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

const CONF_TITLE: Record<Confidence, string> = {
  observed: "GPS points on this road",
  high: "Inferred across a gap in the GPS track; the route fits the time or your usual roads",
  low: "Inferred across a gap in the GPS track; low confidence",
};

function RoadList({ roads, onRoad }: { roads: RoadSeg[]; onRoad?: (key: string) => void }) {
  if (!roads.length) return <span className="muted">not matched</span>;
  return (
    <span className="roadseq">
      {roads.map((r, i) => (
        <span key={i}>
          {i > 0 && <span className="arrow"> → </span>}
          <span
            className={`seg conf-${r.confidence}${r.new ? " new" : ""}${onRoad ? " link" : ""}`}
            title={(onRoad ? "Show every time you drove this road · " : "") + CONF_TITLE[r.confidence] + (r.new ? " · first time on this road" : "")}
            onClick={onRoad ? (e) => { e.stopPropagation(); onRoad(r.road_key); } : undefined}
          >
            {r.label}
          </span>
        </span>
      ))}
    </span>
  );
}

export default function App() {
  const mapDiv = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [basemap, setBasemap] = useState<Basemap>(() =>
    hashParams().has("sat") ? "satellite" : hashParams().has("map") ? "map" : stored("drivelog-basemap", ["map", "satellite"] as const, "map"));
  const [freq, setFreq] = useState<Freq>(() => {
    const f = hashParams().get("freq");
    return f === "mi" || f === "drives" || f === "off" ? f : stored("drivelog-freq", ["drives", "mi", "off"] as const, "drives");
  });
  const ground = groundOf(theme, basemap);
  const groundRef = useRef<Theme>(ground);
  groundRef.current = ground;
  const [yrange, setYrange] = useState<[number, number]>([0, 0]); // indexes into trafficYears
  const [month, setMonth] = useState(0); // explore: index into explore.months
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [roads, setRoads] = useState<GeoJSON.FeatureCollection | null>(null);
  const [trav, setTrav] = useState<Traversals | null>(null);
  const [road, setRoad] = useState<Road | null>(null);
  const [roadLoading, setRoadLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchResult | null>(null);
  const [area, setArea] = useState<Area | null>(null);
  const [scope, setScope] = useState<Scope>({ kind: "all" });
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number | null>(null);
  const [mode, setMode] = useState<Mode>("roads");
  const [backMode, setBackMode] = useState<Mode>("roads");
  const [day, setDay] = useState<string>("");
  const [dayDrives, setDayDrives] = useState<DayDrive[] | null>(null);
  const [drive, setDrive] = useState<DriveDetail | null>(null);
  const [layers, setLayers] = useState<MapLayer[]>([]);
  const [styleTick, setStyleTick] = useState(0); // bumps on each basemap style load
  const [layersVer, setLayersVer] = useState(0); // bumps once our map layers are installed on that style
  const [loadMs, setLoadMs] = useState<number | null>(null);
  const t0 = useRef(performance.now());
  const appliedStyle = useRef(`${theme}:${basemap}`);
  const themeRef = useRef<Theme>(theme);
  themeRef.current = theme;
  const loadTimed = useRef(false);
  const skipSearch = useRef(false);
  const dayLayerKey = useRef<string | null>(null); // the layer Day view put on the map for the current date

  // ---------------------------------------------------------------- data
  useEffect(() => {
    api.years().then((ys) => {
      setYears(ys);
      setYear(ys[ys.length - 1] ?? null);
    });
    Promise.all([api.meta(), api.roads(), api.traversals()]).then(([m, r, t]) => {
      performance.mark("drivelog:data");
      setMeta(m);
      if (m.bbox) mapRef.current?.fitBounds(m.bbox, { padding: PAD, animate: false });
      setRoads(r);
      setTrav(t);
      const ys = new Set(t.days.map((d) => d.slice(0, 4)));
      setYrange([0, Math.max(0, ys.size - 1)]);
      setDay(t.days[t.days.length - 1] ?? "");
    });
  }, []);

  // Years with driving, for the year-range slider.
  const trafficYears = useMemo(() => (trav ? [...new Set(trav.days.map((d) => +d.slice(0, 4)))] : []), [trav]);
  // The slider's years as a range of day indexes (trav.days is sorted).
  const range = useMemo<[number, number]>(() => {
    if (!trav || !trafficYears.length) return [0, 0];
    const lo = String(trafficYears[yrange[0]] ?? trafficYears[0]);
    const hi = String(trafficYears[yrange[1]] ?? trafficYears[trafficYears.length - 1]);
    let a = trav.days.findIndex((d) => d.startsWith(lo));
    let b = -1;
    for (let i = trav.days.length - 1; i >= 0; i--) if (trav.days[i].startsWith(hi)) { b = i; break; }
    if (a < 0) a = 0;
    if (b < 0) b = trav.days.length - 1;
    return [a, b];
  }, [trav, trafficYears, yrange]);
  const since = trav?.days[range[0]];
  const until = trav?.days[range[1]];

  // Drives and miles per road within the year range: they colour the frequency map, decide which roads
  // are clickable, and give the range summary.
  const counts = useMemo(() => {
    if (!trav || !roads) return null;
    const c = new Uint32Array(roads.features.length);
    const m = new Float64Array(roads.features.length);
    const drives = new Set<number>();
    let total = 0;
    const [lo, hi] = range;
    for (let i = 0; i < trav.feature.length; i++) {
      const d = trav.day[i];
      if (d < lo || d > hi) continue;
      const f = trav.feature[i];
      c[f]++;
      m[f] += trav.length[i] / 1609.344;
      drives.add(trav.drive[i]);
      total += trav.length[i];
    }
    let maxC = 0;
    let maxM = 0;
    for (let i = 0; i < c.length; i++) {
      if (c[i] > maxC) maxC = c[i];
      if (m[i] > maxM) maxM = m[i];
    }
    return { c, m, maxC, maxM, drives: drives.size, mi: total / 1609.344 };
  }, [trav, roads, range]);

  // Explored over time: the month each road was first driven, and the running total of distinct road
  // miles (each way counted once, at its longest traversal, as in the Year tab's "new miles").
  const explore = useMemo(() => {
    if (!trav || !roads || !trav.days.length) return null;
    const n = roads.features.length;
    const firstDay = new Int32Array(n).fill(-1);
    const len = new Float64Array(n);
    for (let i = 0; i < trav.feature.length; i++) {
      const f = trav.feature[i];
      const d = trav.day[i];
      if (firstDay[f] < 0 || d < firstDay[f]) firstDay[f] = d;
      if (trav.length[i] > len[f]) len[f] = trav.length[i];
    }
    const months: string[] = [];
    let [y, mo] = trav.days[0].split("-").map(Number);
    const last = trav.days[trav.days.length - 1].slice(0, 7);
    for (;;) {
      const key = `${y}-${String(mo).padStart(2, "0")}`;
      months.push(key);
      if (key >= last) break;
      mo = mo === 12 ? 1 : mo + 1;
      if (mo === 1) y++;
    }
    const idx = new Map(months.map((k, i) => [k, i]));
    const fm = new Int32Array(n).fill(1e9);
    const addMi = new Float64Array(months.length);
    const addN = new Uint32Array(months.length);
    for (let f = 0; f < n; f++) {
      if (firstDay[f] < 0) continue;
      const k = idx.get(trav.days[firstDay[f]].slice(0, 7))!;
      fm[f] = k;
      addMi[k] += len[f] / 1609.344;
      addN[k]++;
    }
    const cumMi = new Float64Array(months.length);
    const cumN = new Uint32Array(months.length);
    for (let k = 0; k < months.length; k++) {
      cumMi[k] = (k ? cumMi[k - 1] : 0) + addMi[k];
      cumN[k] = (k ? cumN[k - 1] : 0) + addN[k];
    }
    return { months, fm, addN, cumMi, cumN };
  }, [trav, roads]);

  // Play: one month per tick, 3 months a second at 1x.
  useEffect(() => {
    if (!playing || !explore) return;
    const h = setInterval(() => {
      setMonth((m) => {
        if (m >= explore.months.length - 1) {
          setPlaying(false);
          return m;
        }
        return m + 1;
      });
    }, 1000 / (3 * speed));
    return () => clearInterval(h);
  }, [playing, speed, explore]);

  // ---------------------------------------------------------------- map setup
  useEffect(() => {
    if (!mapDiv.current) return;
    const map = new maplibregl.Map({
      container: mapDiv.current,
      style: styleFor(theme, basemap),
      center: [0, 20],
      zoom: 1.5, // until /api/meta says where the drives are
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial", maxWidth: 110 }), "bottom-right");
    map.on("style.load", () => setStyleTick((x) => x + 1));
    mapRef.current = map;
    (window as unknown as { __drivelogMap: MLMap }).__drivelogMap = map; // for debugging in devtools
    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem("drivelog-theme", theme);
      localStorage.setItem("drivelog-basemap", basemap);
      localStorage.setItem("drivelog-freq", freq);
    } catch {
      /* ignore */
    }
    document.documentElement.dataset.theme = theme;
    const map = mapRef.current;
    const key = `${theme}:${basemap}`;
    if (map && appliedStyle.current !== key) {
      appliedStyle.current = key;
      map.setStyle(styleFor(theme, basemap), { diff: false });
    }
  }, [theme, basemap, freq]);

  // Install sources and layers after each basemap style load. Only runs after a style.load
  // (styleTick), never on a bare theme change, and never writes styleTick, so it can't loop.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !roads || !styleTick || map.getSource("roads")) return;
    const theme = groundRef.current; // overlay colours follow the ground (imagery counts as dark)
    const casing = theme === "dark" ? "#0b0f17" : "#ffffff";
    const offset = ["get", "offset"] as unknown as maplibregl.ExpressionSpecification;
    map.addSource("roads", { type: "geojson", data: roads });
    map.addSource("routes", { type: "geojson", data: EMPTY });
    map.addSource("new", { type: "geojson", data: EMPTY });
    map.addSource("area-outline", { type: "geojson", data: EMPTY });
    // The open area's boundary: a light fill and a dashed edge, under everything else we draw.
    map.addLayer({ id: "area-fill", type: "fill", source: "area-outline",
      paint: { "fill-color": OUTLINE[theme], "fill-opacity": 0.06 } });
    map.addLayer({ id: "area-edge", type: "line", source: "area-outline",
      paint: { "line-color": OUTLINE[theme], "line-width": 2, "line-dasharray": [2, 1.5], "line-opacity": 0.8 } });
    // Frequency map: every road driven in the year range, coloured and weighted by how often (paint set
    // by the effect below). The busiest roads draw on top.
    map.addLayer({ id: "freq", type: "line", source: "roads",
      layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["get", "dc"] },
      paint: { "line-opacity": 0 } });
    // Explored over time: roads appear in the month they were first driven (paint set below).
    map.addLayer({ id: "explored", type: "line", source: "roads",
      layout: { "line-cap": "round", "line-join": "round", visibility: "none" },
      paint: { "line-opacity": 0 } });
    // Every road you've driven, invisible: the map stays clean but roads are still clickable.
    map.addLayer({ id: "roads", type: "line", source: "roads",
      paint: { "line-color": "#000", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3, 14, 8], "line-opacity": 0 } });
    // The searched road: a thin neutral outline under the routes.
    map.addLayer({ id: "road-outline", type: "line", source: "roads", filter: ["in", ["get", "way_id"], ["literal", []]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": OUTLINE[theme], "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.5, 14, 4], "line-opacity": 0.55 } });
    // New roads on the open drive glow underneath (Drive details only).
    map.addLayer({ id: "new-glow", type: "line", source: "new", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": NEW_COLOR, "line-width": 16, "line-opacity": 0.5, "line-blur": 2 } });
    // Everything on the map, one colour per day / drive; inferred stretches dashed.
    map.addLayer({ id: "routes-casing", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": casing, "line-width": 7.5, "line-opacity": 0.85, "line-offset": offset } });
    map.addLayer({ id: "routes-observed", type: "line", source: "routes", filter: ["==", ["get", "inferred"], false],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 4.5, "line-offset": offset } });
    map.addLayer({ id: "routes-inferred", type: "line", source: "routes", filter: ["==", ["get", "inferred"], true],
      layout: { "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 4, "line-dasharray": [1.4, 1.2], "line-offset": offset,
        "line-opacity": ["match", ["get", "confidence"], "low", 0.55, 0.95] } });
    map.on("mouseenter", "roads", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "roads", () => (map.getCanvas().style.cursor = ""));
    setLayersVer((v) => v + 1);
  }, [roads, styleTick]);

  // Push drive counts and miles into feature state (roads with no drives in the range aren't clickable),
  // plus the month each road was first driven (explore).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !counts || !map.getSource("roads")) return;
    for (let i = 0; i < counts.c.length; i++)
      map.setFeatureState({ source: "roads", id: i }, { c: counts.c[i], m: counts.m[i], fm: explore ? explore.fm[i] : 1e9 });
    if (!loadTimed.current) {
      loadTimed.current = true;
      const done = () => {
        performance.mark("drivelog:ready");
        setLoadMs(Math.round(performance.now() - t0.current));
      };
      if (map.isSourceLoaded("roads")) done();
      else {
        const onData = (e: maplibregl.MapSourceDataEvent) => {
          if (e.sourceId === "roads" && map.isSourceLoaded("roads")) {
            map.off("sourcedata", onData);
            done();
          }
        };
        map.on("sourcedata", onData);
      }
    }
  }, [counts, explore, layersVer]);

  // Frequency map paint: a log scale normalised to the busiest road in the range (so a short range still
  // uses the whole ramp), on drive count or miles. It dims while routes are on the map so they stay legible.
  const freqOn = freq !== "off" && mode === "roads"; // other views have their own layers on the map
  const freqMax = counts ? (freq === "mi" ? counts.maxM : counts.maxC) : 0;
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("freq")) return;
    map.setLayoutProperty("freq", "visibility", freqOn ? "visible" : "none");
    if (!freqOn) return;
    const key = freq === "mi" ? "m" : "c";
    const v = ["coalesce", ["feature-state", key], 0] as unknown as maplibregl.ExpressionSpecification;
    const t = ["/", ["ln", ["+", 1, v]], Math.log(1 + Math.max(freqMax, 1))] as unknown as maplibregl.ExpressionSpecification;
    const r = RAMP[ground];
    map.setPaintProperty("freq", "line-color", ["interpolate", ["linear"], t, 0, r[0], 0.25, r[1], 0.5, r[2], 0.75, r[3], 1, r[4]]);
    map.setPaintProperty("freq", "line-width", ["interpolate", ["linear"], ["zoom"],
      6, ["+", 0.5, ["*", 1.5, t]], 10, ["+", 0.8, ["*", 3, t]], 15, ["+", 1.5, ["*", 7, t]]]);
    map.setPaintProperty("freq", "line-opacity", ["case", [">", v, 0], layers.length ? 0.35 : 0.92, 0]);
  }, [freqOn, freq, freqMax, ground, layers.length, layersVer]);

  // Explore paint: roads first driven up to the current month; that month's new ones highlighted.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("explored")) return;
    map.setLayoutProperty("explored", "visibility", mode === "explore" ? "visible" : "none");
    if (mode !== "explore") return;
    const fm = ["coalesce", ["feature-state", "fm"], 1e9] as unknown as maplibregl.ExpressionSpecification;
    const fresh = ["==", fm, month] as unknown as maplibregl.ExpressionSpecification;
    map.setPaintProperty("explored", "line-opacity", ["case", ["<=", fm, month], 0.95, 0]);
    map.setPaintProperty("explored", "line-color", ["case", fresh, NEW_COLOR, RAMP[ground][2]]);
    map.setPaintProperty("explored", "line-width", ["interpolate", ["linear"], ["zoom"],
      6, ["case", fresh, 4, 1], 12, ["case", fresh, 7, 2.2], 15, ["case", fresh, 10, 4]]);
  }, [mode, month, ground, layersVer]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("road-outline")) return;
    map.setFilter("road-outline", ["in", ["get", "way_id"], ["literal", mode === "roads" ? road?.way_ids ?? [] : []]]);
    map.setLayoutProperty("new-glow", "visibility", mode === "drive" ? "visible" : "none");
  }, [road, mode, layersVer]);

  const fitTo = useCallback((fc: GeoJSON.FeatureCollection) => {
    const b = new maplibregl.LngLatBounds();
    const add = (c: unknown): void => {
      if (Array.isArray(c) && typeof c[0] === "number") b.extend(c as [number, number]);
      else if (Array.isArray(c)) c.forEach(add);
    };
    fc.features.forEach((f) => add((f.geometry as GeoJSON.LineString | GeoJSON.MultiLineString).coordinates));
    if (!b.isEmpty()) mapRef.current?.fitBounds(b, { padding: PAD, maxZoom: 15 });
  }, []);

  // ---------------------------------------------------------------- map layers (days and drives)
  useEffect(() => {
    const feats = layers.flatMap((l, i) =>
      l.fc.features.map((f) => ({ ...f, properties: { ...f.properties, color: l.color, offset: OFFSETS[i % OFFSETS.length] } })));
    (mapRef.current?.getSource("routes") as maplibregl.GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features: feats });
  }, [layers, layersVer]);

  const nextColor = (cur: MapLayer[]) => {
    const used = new Set(cur.map((l) => l.color));
    return COLORS.find((c) => !used.has(c)) ?? COLORS[cur.length % COLORS.length];
  };

  type LayerSpec = Omit<MapLayer, "color" | "fc"> & { load?: () => Promise<GeoJSON.FeatureCollection> };

  const addLayer = async ({ load, ...spec }: LayerSpec, opts: { fit?: boolean; replaceKey?: string } = {}) => {
    const fc = await (load ? load() : api.drives(spec.driveIds));
    setLayers((cur) => {
      const without = cur.filter((l) => l.key !== spec.key);
      const at = opts.replaceKey ? without.findIndex((l) => l.key === opts.replaceKey) : -1;
      // Replacing (Day view moving to another date) keeps the slot and its colour.
      if (at >= 0) return without.map((l, i) => (i === at ? { ...spec, fc, color: l.color } : l));
      return [...without, { ...spec, fc, color: nextColor(without) }];
    });
    if (opts.fit !== false) fitTo(fc);
  };

  const removeLayer = (key: string) => {
    setLayers((cur) => cur.filter((l) => l.key !== key));
    if (dayLayerKey.current === key) dayLayerKey.current = null;
  };

  const toggleLayer = (spec: LayerSpec) => {
    if (layers.some((l) => l.key === spec.key)) removeLayer(spec.key);
    else addLayer(spec);
  };

  const colorOfDrive = (id: string) => layers.find((l) => l.driveIds.includes(id))?.color;
  const colorOfKey = (key: string) => layers.find((l) => l.key === key)?.color;

  // ---------------------------------------------------------------- roads
  const openRoad = useCallback(
    async (p: { way_id?: string; key?: string }) => {
      setMode("roads");
      setRoadLoading(true);
      try {
        setRoad(await api.road({ ...p, since, until }));
      } finally {
        setRoadLoading(false);
      }
    },
    [since, until],
  );

  // Clicking the map opens the road under the cursor.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const onClick = (e: maplibregl.MapMouseEvent) => {
      if (!map.getLayer("roads")) return;
      const box: [maplibregl.PointLike, maplibregl.PointLike] = [
        [e.point.x - 5, e.point.y - 5],
        [e.point.x + 5, e.point.y + 5],
      ];
      const hits = map.queryRenderedFeatures(box, { layers: ["roads"] }).filter((f) => (f.state as { c?: number }).c);
      // Several roads can fall inside the box at low zoom: take the one nearest the cursor.
      let best: (typeof hits)[number] | undefined;
      let bestD = Infinity;
      for (const f of hits) {
        const coords = (f.geometry as GeoJSON.LineString).coordinates;
        for (let i = 1; i < coords.length; i++) {
          const d = segDist(e.point, map.project(coords[i - 1] as [number, number]), map.project(coords[i] as [number, number]));
          if (d < bestD) {
            bestD = d;
            best = f;
          }
        }
      }
      if (best) openRoad({ way_id: String(best.properties.way_id) });
    };
    map.on("click", onClick);
    return () => {
      map.off("click", onClick);
    };
  }, [openRoad]);

  // Refresh the open road when the date range changes.
  useEffect(() => {
    if (!road) return;
    const h = setTimeout(() => openRoad(road.road_key ? { key: road.road_key } : { way_id: road.way_ids[0] }), 250);
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [since, until]);

  // ---------------------------------------------------------------- drive details
  const openDrive = useCallback(
    async (id: string) => {
      const d = await api.drive(id);
      if (mode !== "drive") setBackMode(mode);
      setMode("drive");
      setDrive(d);
      (mapRef.current?.getSource("new") as maplibregl.GeoJSONSource | undefined)?.setData({
        type: "FeatureCollection",
        features: d.geojson.features.filter((f) => f.properties?.new),
      });
      // Make sure the drive is on the map (it may already be, as part of a day).
      if (!layers.some((l) => l.driveIds.includes(id))) {
        addLayer({ key: `drive:${id}`, kind: "drive", label: d.date, sub: d.start, driveIds: [id] });
      } else {
        fitTo(d.geojson);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, layers, fitTo],
  );

  // ---------------------------------------------------------------- search
  useEffect(() => {
    if (skipSearch.current) {
      skipSearch.current = false; // query was set by picking a hit, not by typing
      return;
    }
    if (query.trim().length < 1) {
      setHits(null);
      return;
    }
    const h = setTimeout(() => api.search(query.trim()).then(setHits).catch(() => setHits({ roads: [], areas: [] })), 200);
    return () => clearTimeout(h);
  }, [query]);

  const pickHit = (h: SearchHit) => {
    setHits(null);
    skipSearch.current = true;
    setQuery(h.label);
    if (h.bbox) mapRef.current?.fitBounds(h.bbox, { padding: PAD, maxZoom: 15 });
    openRoad({ key: h.road_key });
  };

  // ---------------------------------------------------------------- areas
  // Opening an area outlines its boundary, adds its driven roads as a layer in the next colour, and
  // opens a panel with its stats and dates. A date adds that day's drives inside the area.
  const openArea = async (id: string) => {
    const [a, shape] = await Promise.all([api.area(id), api.areaShape(id)]);
    setArea(a);
    setMode("area");
    (mapRef.current?.getSource("area-outline") as maplibregl.GeoJSONSource | undefined)?.setData(shape);
    fitTo({ type: "FeatureCollection", features: [shape] });
    if (!layers.some((l) => l.key === `area:${id}`) && a.drive_count > 0) {
      addLayer({ key: `area:${id}`, kind: "area", label: a.label, sub: `roads driven · ${fmtMi(a.mi, 0)} mi`, driveIds: [],
        load: () => api.areaRoads(id) }, { fit: false });
    }
  };

  const pickArea = (h: AreaHit) => {
    setHits(null);
    skipSearch.current = true;
    setQuery(h.label);
    openArea(h.area_id);
  };

  useEffect(() => {
    // The boundary shows only while its panel is open.
    if (mode !== "area") (mapRef.current?.getSource("area-outline") as maplibregl.GeoJSONSource | undefined)?.setData(EMPTY);
  }, [mode]);

  // ---------------------------------------------------------------- day view
  // Showing a day puts the whole day on the map as one layer, in one colour. Moving to another date
  // replaces that layer (keeping its colour); leaving Day view leaves it on the map.
  useEffect(() => {
    if (mode !== "day" || !day) return;
    let cancelled = false;
    api.day(day).then((ds) => {
      if (cancelled) return;
      setDayDrives(ds);
      const key = `day:${day}`;
      if (!ds.length || layers.some((l) => l.key === key)) return;
      const miles = ds.reduce((s, d) => s + d.matched_mi, 0);
      addLayer(
        { key, kind: "day", label: day, sub: `${ds.length} drive${ds.length === 1 ? "" : "s"} · ${fmtMi(miles)} mi`, driveIds: ds.map((d) => d.drive_id) },
        { replaceKey: dayLayerKey.current ?? undefined },
      );
      dayLayerKey.current = key;
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, day]);

  const shiftDay = (delta: number) => {
    if (!trav) return;
    const i = trav.days.indexOf(day);
    const j = i === -1 ? trav.days.findIndex((d) => d > day) : i + delta;
    if (j >= 0 && j < trav.days.length) setDay(trav.days[j]);
  };

  // ---------------------------------------------------------------- deep links
  // #day=2016-03-05 · #road=HI%2072 · #area=zip:96734 · #patterns · #year=2016 open that view on load.
  const linked = useRef(false);
  useEffect(() => {
    if (linked.current || !trav || !layersVer) return;
    linked.current = true;
    const h = hashParams();
    const yrs = h.get("years")?.split("-").map(Number); // #years=2019-2022
    if (yrs && trafficYears.length) {
      const a = trafficYears.indexOf(yrs[0]);
      const b = trafficYears.indexOf(yrs[1] ?? yrs[0]);
      if (a >= 0 && b >= a) setYrange([a, b]);
    }
    if (h.get("day")) {
      setDay(h.get("day")!);
      setMode("day");
    } else if (h.get("road")) openRoad({ key: h.get("road")! });
    else if (h.get("area")) openArea(h.get("area")!);
    else if (h.has("patterns")) setMode("patterns");
    else if (h.has("year")) {
      if (h.get("year")) setYear(+h.get("year")!);
      setMode("year");
    } else if (h.has("explore") && explore) {
      const k = explore.months.indexOf(h.get("explore") ?? "");
      setMonth(k >= 0 ? k : explore.months.length - 1);
      setMode("explore");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trav, layersVer]);

  const confMark = (c: Confidence) => (c === "observed" ? "" : c === "high" ? " ~" : " ?");
  const dayColor = colorOfKey(`day:${day}`);

  // ---------------------------------------------------------------- render
  return (
    <div className="app">
      <div ref={mapDiv} className="map" />

      <header className="topbar panel">
        <div className="brand">Drivelog</div>
        <div className="search">
          <input
            placeholder="Search a road, zip code or place: Kailua Rd, 72, 96734, a town…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || !hits) return;
              if (hits.roads[0]) pickHit(hits.roads[0]);
              else if (hits.areas[0]) pickArea(hits.areas[0]);
            }}
          />
          {hits && (
            <ul className="hits">
              {hits.roads.length + hits.areas.length === 0 && <li className="muted">No matching roads or places</li>}
              {hits.areas.length > 0 && <li className="group muted">Places</li>}
              {hits.areas.map((h) => (
                <li key={h.area_id} onClick={() => pickArea(h)}>
                  <b>{h.label}</b>
                  <span className="count">{h.kind === "zip" ? "zip" : h.kind} · {h.mi > 0 ? `${fmtMi(h.mi, 0)} mi` : "not driven"}</span>
                </li>
              ))}
              {hits.roads.length > 0 && <li className="group muted">Roads</li>}
              {hits.roads.map((h) => (
                <li key={h.road_key} onClick={() => pickHit(h)}>
                  <b>{h.label}</b>
                  {h.names?.[0] && h.names[0] !== h.label && <span className="muted"> · {h.names[0]}</span>}
                  {/* Same-named streets in different towns: years driven tell them apart. */}
                  {hits.roads.filter((o) => o.label === h.label).length > 1 && h.first_date && (
                    <span className="muted"> · {h.first_date.slice(0, 4)}{h.last_date && h.last_date.slice(0, 4) !== h.first_date.slice(0, 4) ? `–${h.last_date.slice(0, 4)}` : ""}</span>
                  )}
                  <span className="count">{h.drive_count.toLocaleString()} drives</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="tabs">
          <button className={mode === "roads" ? "on" : ""} onClick={() => setMode("roads")}>Roads</button>
          <button className={mode === "explore" ? "on" : ""} title="Roads appearing the month you first drove them"
            onClick={() => { if (mode !== "explore" && explore) setMonth(explore.months.length - 1); setMode("explore"); }}>Explored</button>
          <button className={mode === "day" ? "on" : ""} onClick={() => setMode("day")}>Day</button>
          <button className={mode === "patterns" ? "on" : ""} onClick={() => setMode("patterns")}>Patterns</button>
          <button className={mode === "year" ? "on" : ""} onClick={() => setMode("year")}>Year</button>
          {drive && <button className={mode === "drive" ? "on" : ""} onClick={() => setMode("drive")}>Drive</button>}
          {area && <button className={mode === "area" ? "on" : ""} onClick={() => openArea(area.area_id)}>Area</button>}
        </div>
        <button className={`theme basemap${basemap === "satellite" ? " on" : ""}`} onClick={() => setBasemap(basemap === "map" ? "satellite" : "map")}
          title={basemap === "map" ? "Satellite imagery" : "Street map"}>{basemap === "map" ? "Satellite" : "Map"}</button>
        <button className="theme" onClick={() => setTheme(theme === "light" ? "dark" : "light")} title="Toggle theme">
          {theme === "light" ? "☾" : "☀"}
        </button>
      </header>

      {layers.length > 0 && (
        <div className="onmap panel">
          <div className="onmap-head">
            <h3>On the map</h3>
            <button className="linkbtn" onClick={() => { setLayers([]); dayLayerKey.current = null; }}>Clear all</button>
          </div>
          <ul>
            {layers.map((l) => (
              <li key={l.key}>
                <span className="dot" style={{ background: l.color }} />
                <span className="what">
                  {l.kind === "area" || l.kind === "newroads" ? l.label : l.kind === "day" ? fmtDate(l.label) : `${fmtDate(l.label)} ${l.sub}`}
                  {l.kind !== "drive" && <span className="muted"> · {l.sub}</span>}
                </span>
                {l.kind === "area" ? (
                  <button className="linkbtn" onClick={() => openArea(l.key.slice(5))} title="Open this area">area</button>
                ) : l.kind === "newroads" ? (
                  <button className="linkbtn" onClick={() => { setYear(+l.key.slice(4)); setMode("year"); }} title="Open this year">year</button>
                ) : l.kind === "drive" ? (
                  <button className="linkbtn" onClick={() => openDrive(l.driveIds[0])} title="Route, new roads, similar drives">details</button>
                ) : (
                  <button className="linkbtn" onClick={() => { setDay(l.label); dayLayerKey.current = l.key; setMode("day"); }} title="Open this day">day</button>
                )}
                <button className="x" onClick={() => removeLayer(l.key)} title="Remove from map">×</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {mode === "roads" && trav && (
        <div className="range panel">
          <div className="range-head">
            <div className="seg-toggle small" role="group" aria-label="Colour roads by">
              <button className={freq === "drives" ? "on" : ""} onClick={() => setFreq("drives")} title="Colour roads by how many drives used them">Drives</button>
              <button className={freq === "mi" ? "on" : ""} onClick={() => setFreq("mi")} title="Colour roads by miles driven on them">Miles</button>
              <button className={freq === "off" ? "on" : ""} onClick={() => setFreq("off")} title="Plain map">Off</button>
            </div>
            <span className="muted">
              {counts ? `${counts.drives.toLocaleString()} drives · ${Math.round(counts.mi).toLocaleString()} mi` : ""}
            </span>
          </div>
          {freqOn && counts && freqMax > 0 && (
            <div className="freq-legend" aria-label={`Colour scale: ${freq === "mi" ? "miles" : "drives"} per road`}>
              <span className="bar" style={{ background: `linear-gradient(90deg, ${RAMP[ground].join(", ")})` }} />
              <div className="ticks">
                {[0, 0.5, 1].map((t) => {
                  const v = Math.exp(t * Math.log(1 + freqMax)) - 1;
                  const lab = freq === "mi" ? (v < 1 ? "< 1 mi" : `${v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString()} mi`) : `${Math.max(1, Math.round(v)).toLocaleString()}`;
                  return <span key={t} style={{ left: `${t * 100}%` }}>{lab}{t === 1 ? (freq === "mi" ? "" : " drives") : ""}</span>;
                })}
              </div>
              <div className="legend-note muted">{freq === "mi" ? "miles driven on each road" : "drives on each road"} · log scale · click a road for its dates</div>
            </div>
          )}
          {trafficYears.length > 1 ? (
            <>
              <div className="dual years" title="Years shown on the map">
                <input type="range" min={0} max={trafficYears.length - 1} value={yrange[0]} aria-label="First year"
                  onChange={(e) => setYrange([Math.min(+e.target.value, yrange[1]), yrange[1]])} />
                <input type="range" min={0} max={trafficYears.length - 1} value={yrange[1]} aria-label="Last year"
                  onChange={(e) => setYrange([yrange[0], Math.max(+e.target.value, yrange[0])])} />
              </div>
              <div className="year-ticks">
                {trafficYears.map((y, i) => (
                  <button key={y} className={i >= yrange[0] && i <= yrange[1] ? "in" : ""} style={{ left: `${(i / (trafficYears.length - 1)) * 100}%` }}
                    onClick={() => setYrange([i, i])} title={`Only ${y}`}>{trafficYears.length > 12 && i % 2 ? "" : y}</button>
                ))}
              </div>
            </>
          ) : (
            <div className="muted small">{trafficYears[0]}</div>
          )}
        </div>
      )}

      {mode === "explore" && explore && (
        <>
          <div className="counter panel" aria-live="off">
            <div className="big">{Math.round(explore.cumMi[month]).toLocaleString()}</div>
            <div className="muted">miles of road explored</div>
            <div className="small muted">{explore.cumN[month].toLocaleString()} road segments · +{explore.addN[month].toLocaleString()} this month</div>
          </div>
          <div className="range panel explore">
            <div className="range-head">
              <button className="play" onClick={() => {
                if (!playing && month >= explore.months.length - 1) setMonth(0);
                setPlaying(!playing);
              }} title={playing ? "Pause" : "Play"} aria-label={playing ? "Pause" : "Play"}>{playing ? "❚❚" : "▶"}</button>
              <b className="month">{new Date(explore.months[month] + "-15").toLocaleDateString(undefined, { month: "long", year: "numeric" })}</b>
              <div className="seg-toggle small" role="group" aria-label="Speed">
                {[1, 2, 4].map((x) => (
                  <button key={x} className={speed === x ? "on" : ""} onClick={() => setSpeed(x)}>{x}×</button>
                ))}
              </div>
            </div>
            <input className="scrub" type="range" min={0} max={explore.months.length - 1} value={month} aria-label="Month"
              onChange={(e) => { setPlaying(false); setMonth(+e.target.value); }} />
            <div className="range-labels muted"><span>{explore.months[0]}</span><span>{explore.months[explore.months.length - 1]}</span></div>
            <div className="legend-note muted">
              <span className="swatch" style={{ background: NEW_COLOR }} /> first driven this month
              <span className="swatch" style={{ background: RAMP[ground][2], marginLeft: 10 }} /> earlier
            </div>
          </div>
        </>
      )}

      {mode === "roads" && (road || roadLoading) && (
        <aside className="side panel">
          <button className="close" onClick={() => setRoad(null)} title="Close road">×</button>
          {road && (
            <>
              <h2>{road.label}</h2>
              <div className="sub muted">
                {[road.ref && road.ref !== road.label ? road.ref : null, ...(road.names ?? []).filter((n) => n !== road.label).slice(0, 2)]
                  .filter(Boolean).join(" · ") || " "}
              </div>
              <dl className="stats">
                <div><dt>Drives</dt><dd>{road.drive_count.toLocaleString()}</dd></div>
                <div><dt>Days</dt><dd>{road.distinct_days.toLocaleString()}</dd></div>
                <div><dt>Total mi</dt><dd>{fmtMi(road.total_mi, 0)}</dd></div>
                <div><dt>First</dt><dd>{road.first_date ?? "–"}</dd></div>
                <div><dt>Last</dt><dd>{road.last_date ?? "–"}</dd></div>
                <div title="Drives with GPS points on this road (the rest were inferred across gaps)">
                  <dt>GPS-confirmed</dt><dd>{road.observed_drive_count.toLocaleString()}</dd>
                </div>
                {road.road_key && (
                  <div><dt>Patterns</dt><dd><button className="linkbtn" onClick={() => { setScope({ kind: "road", key: road.road_key!, label: road.label }); setMode("patterns"); }}>when ›</button></dd></div>
                )}
              </dl>
              <h3>Dates <span className="muted">(newest first · click a time to add that route to the map)</span></h3>
              <ul className="dates">
                {road.dates.map((d) => {
                  const rowKey = `roadday:${road.road_key ?? road.way_ids[0]}:${d.date}`;
                  const rowColor = colorOfKey(rowKey);
                  return (
                    <li key={d.date} className={`conf-${d.confidence}`} title="Add all of this date's drives on this road as one color"
                      onClick={() => toggleLayer({ key: rowKey, kind: "day", label: d.date,
                        sub: `${d.drives.length} drive${d.drives.length === 1 ? "" : "s"} on ${road.label}`, driveIds: d.drive_ids })}>
                      <span className="date">
                        {rowColor && <span className="dot" style={{ background: rowColor }} />}
                        {d.date}
                      </span>
                      <span className="chips">
                        {d.drives.map((x) => {
                          const c = colorOfDrive(x.drive_id);
                          return (
                            <button key={x.drive_id} className={`chip conf-${x.confidence}${c ? " sel" : ""}`}
                              style={c ? { background: c, borderColor: c } : undefined} title={CONF_TITLE[x.confidence]}
                              onClick={(e) => {
                                e.stopPropagation();
                                toggleLayer({ key: `drive:${x.drive_id}`, kind: "drive", label: d.date, sub: x.start, driveIds: [x.drive_id] });
                              }}>
                              {x.start}{confMark(x.confidence)}
                            </button>
                          );
                        })}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {roadLoading && !road && <div className="muted">Loading…</div>}
        </aside>
      )}

      {mode === "day" && (
        <aside className="side panel">
          <div className="daypick">
            <button onClick={() => shiftDay(-1)} title="Previous day with drives">‹</button>
            <input type="date" value={day} min={trav?.days[0]} max={trav?.days[trav.days.length - 1]} onChange={(e) => setDay(e.target.value)} />
            <button onClick={() => shiftDay(1)} title="Next day with drives">›</button>
          </div>
          {dayDrives && dayDrives.length === 0 && <div className="muted">No drives on this day.</div>}
          {dayDrives && dayDrives.length > 0 && (
            <div className="hint muted">Click a road name to see every time you drove it.</div>
          )}
          <ol className="daylist">
            {dayDrives?.map((d) => (
              <li key={d.drive_id} className="clickable" onClick={() => openDrive(d.drive_id)} title="Open this drive">
                <div className="dayhead">
                  <span className="swatch" style={{ background: dayColor ?? "var(--muted)" }} />
                  <b>{d.start}</b>
                  <span className="muted">{fmtMi(d.matched_mi)} mi</span>
                  {d.new_roads > 0 && <span className="tag new">{d.new_roads} new</span>}
                  {d.status !== "ok" && <span className="tag">{d.status.replace(/_/g, " ")}</span>}
                  <span className="open">›</span>
                </div>
                <div className="roadseq-wrap"><RoadList roads={d.roads} onRoad={(key) => openRoad({ key })} /></div>
              </li>
            ))}
          </ol>
        </aside>
      )}

      {mode === "drive" && drive && (
        <aside className="side panel">
          <button className="close" onClick={() => setMode(backMode === "drive" ? "roads" : backMode)} title="Back">‹</button>
          <h2>{fmtDate(drive.date)}</h2>
          <div className="sub muted">
            {drive.start} · {drive.minutes} min{drive.status !== "ok" ? ` · ${drive.status.replace(/_/g, " ")}` : ""}
          </div>
          <dl className="stats">
            <div><dt>Miles</dt><dd>{fmtMi(drive.matched_mi)}</dd></div>
            <div title="Miles on roads you had never driven before this drive"><dt>New mi</dt><dd>{fmtMi(drive.new_mi)}</dd></div>
            <div title="Miles routed across gaps between GPS points"><dt>Inferred mi</dt><dd>{fmtMi(drive.inferred_mi)}</dd></div>
          </dl>
          <h3>Route <span className="muted">(click a road to see every time you drove it)</span></h3>
          <div className="route"><RoadList roads={drive.roads} onRoad={(key) => openRoad({ key })} /></div>
          {drive.new_roads.length > 0 && (
            <>
              <h3>New roads</h3>
              <div className="chips wrap">
                {drive.new_roads.map((k) => (
                  <button key={k} className="chip new" onClick={() => openRoad({ key: k })}>{k.replace(/ #\d+$/, "")}</button>
                ))}
              </div>
            </>
          )}
          <h3>Similar drives <span className="muted">(click to add to the map)</span></h3>
          {drive.similar.length === 0 && <div className="muted">No other drive shares this route.</div>}
          <ul className="similar">
            {drive.similar.map((s) => {
              const c = colorOfDrive(s.drive_id);
              return (
                <li key={s.drive_id} className={c ? "on" : ""} style={c ? { boxShadow: `inset 3px 0 0 ${c}` } : undefined}
                  onClick={() => toggleLayer({ key: `drive:${s.drive_id}`, kind: "drive", label: s.date, sub: s.start, driveIds: [s.drive_id] })}>
                  <span className="when">
                    {c && <span className="dot" style={{ background: c }} />}
                    {s.date} <span className="muted">{s.start}</span>
                  </span>
                  <span className="shared">{fmtMi(s.shared_mi)} mi</span>
                  <span className="pbar" title="Share of this drive's route"><span style={{ width: `${Math.round(s.pct_this * 100)}%` }} /></span>
                  <span className="pct muted">{Math.round(s.pct_this * 100)}%</span>
                  <button className="go" title="Open this drive" onClick={(e) => { e.stopPropagation(); openDrive(s.drive_id); }}>›</button>
                </li>
              );
            })}
          </ul>
        </aside>
      )}

      {mode === "area" && area && (
        <aside className="side panel">
          <button className="close" onClick={() => setMode("roads")} title="Close area">×</button>
          <h2>{area.name}</h2>
          <div className="sub muted">{area.label !== area.name ? area.label.slice(area.name.length).replace(/^ \(|\)$/g, "") + " · " : ""}{area.kind}</div>
          {area.note && <div className="note">{area.note}</div>}
          {area.drive_count === 0 ? (
            <div className="muted" style={{ marginTop: 12 }}>You haven't driven here.</div>
          ) : (
            <>
              <dl className="stats">
                <div><dt>Miles</dt><dd>{fmtMi(area.mi, 0)}</dd></div>
                <div><dt>Drives</dt><dd>{area.drive_count.toLocaleString()}</dd></div>
                <div><dt>Days</dt><dd>{area.distinct_days.toLocaleString()}</dd></div>
                <div><dt>First</dt><dd>{area.first_date ?? "–"}</dd></div>
                <div><dt>Last</dt><dd>{area.last_date ?? "–"}</dd></div>
                <div><dt>Patterns</dt><dd><button className="linkbtn" onClick={() => { setScope({ kind: "area", key: area.area_id, label: area.label }); setMode("patterns"); }}>when ›</button></dd></div>
              </dl>
              <h3>Top roads here</h3>
              <ol className="toproads">
                {area.top_roads.map((r) => (
                  <li key={r.road_key} onClick={() => openRoad({ key: r.road_key })} title="Every time you drove this road">
                    <span className="seg link">{r.label.replace(/ #\d+$/, "")}</span>
                    <span className="muted">{r.drive_count.toLocaleString()} drives · {fmtMi(r.mi)} mi</span>
                  </li>
                ))}
              </ol>
              <h3>Dates <span className="muted">(click to add that day's driving here to the map)</span></h3>
              <ul className="dates">
                {area.dates.map((d) => {
                  const key = `areaday:${area.area_id}:${d.date}`;
                  const c = colorOfKey(key);
                  return (
                    <li key={d.date} onClick={() => toggleLayer({ key, kind: "day", label: d.date, sub: `in ${area.name}`,
                      driveIds: d.drive_ids, load: () => api.areaDay(area.area_id, d.date) })}>
                      <span className="date">{c && <span className="dot" style={{ background: c }} />}{d.date}</span>
                      <span className="muted">{d.drives} drive{d.drives === 1 ? "" : "s"} · {fmtMi(d.mi)} mi</span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </aside>
      )}

      {mode === "patterns" && (
        <aside className="side panel">
          <Patterns scope={scope} setScope={setScope} />
        </aside>
      )}

      {mode === "year" && (
        <aside className="side panel">
          <Year years={years} year={year} setYear={setYear}
            onRoad={(key) => openRoad({ key })}
            onDay={(d) => { setDay(d); setMode("day"); }}
            onDrive={(id, date, start) => toggleLayer({ key: `drive:${id}`, kind: "drive", label: date, sub: start, driveIds: [id] })}
            onNewRoads={(y, n) => toggleLayer({ key: `new:${y}`, kind: "newroads", label: `New roads in ${y}`,
              sub: `${n.toLocaleString()} roads`, driveIds: [], load: () => api.newRoads(y) })}
            driveColor={colorOfDrive}
            newRoadsColor={year != null ? colorOfKey(`new:${year}`) : undefined} />
        </aside>
      )}

      {meta && (
        <div className="foot muted">
          {meta.drives.toLocaleString()} drives · {Math.round(meta.mi).toLocaleString()} mi · {meta.first_date} → {meta.last_date}
          {loadMs !== null && ` · loaded in ${(loadMs / 1000).toFixed(1)} s`}
        </div>
      )}
    </div>
  );
}
