export type Meta = {
  first_date: string;
  last_date: string;
  drives: number;
  mi: number;
  matched: number;
  bbox: [number, number, number, number] | null;
};

/** How a road use is known: GPS points on it, or only a routed gap fill (and how confidently). */
export type Confidence = "observed" | "high" | "low";

export type Traversals = {
  days: string[];
  drive_ids: string[];
  feature: Uint32Array;
  day: Uint32Array;
  drive: Uint32Array;
  length: Uint32Array;
  observed: Uint8Array;
};

export type DateDrive = { drive_id: string; start: string; confidence: Confidence };
export type RoadDate = { date: string; drives: DateDrive[]; drive_ids: string[]; confidence: Confidence };

export type Road = {
  road_key: string | null;
  label: string;
  ref: string | null;
  names: string[] | null;
  drive_count: number;
  observed_drive_count: number;
  distinct_days: number;
  total_mi: number | null;
  first_date: string | null;
  last_date: string | null;
  way_ids: string[];
  dates: RoadDate[];
};

export type SearchHit = {
  road_key: string;
  label: string;
  ref: string | null;
  names: string[] | null;
  drive_count: number;
  total_mi: number;
  first_date: string | null;
  last_date: string | null;
  bbox: [number, number, number, number] | null;
};

export type RoadSeg = { label: string; road_key: string; mi: number; confidence: Confidence; new: boolean };

export type DayDrive = {
  drive_id: string;
  start: string;
  matched_mi: number;
  status: string;
  new_roads: number;
  roads: RoadSeg[];
  geometry: [number, number][][];
};

export type Similar = {
  drive_id: string;
  date: string;
  start: string;
  shared_mi: number;
  drive_mi: number;
  pct_this: number;
  pct_other: number | null;
};

export type DriveDetail = {
  drive_id: string;
  date: string;
  start: string;
  minutes: number;
  status: string;
  matched_mi: number;
  inferred_mi: number;
  low_confidence_mi: number;
  new_mi: number;
  new_roads: string[];
  roads: RoadSeg[];
  geojson: GeoJSON.FeatureCollection;
  similar: Similar[];
};

export type AreaKind = "zip" | "locality" | "county" | "state" | "country";

export type AreaHit = {
  area_id: string;
  kind: AreaKind;
  name: string;
  label: string;
  county: string | null;
  state_code: string | null;
  country: string | null;
  mi: number;
};

export type Area = AreaHit & {
  drive_count: number;
  distinct_days: number;
  first_date: string | null;
  last_date: string | null;
  note?: string;
  top_roads: { road_key: string; label: string; drive_count: number; mi: number }[];
  dates: { date: string; drive_ids: string[]; drives: number; mi: number }[];
};

export type SearchResult = { roads: SearchHit[]; areas: AreaHit[] };

export type Patterns = {
  filter: { road: string | null; area: string | null };
  days: string[];
  grid: { drives: number[][]; mi: number[][] };
  months: { month: string; drives: number; mi: number }[];
  lengths: { label: string; drives: number }[];
  drives: number;
  mi: number;
  distinct_days: number;
};

type YearTotals = { drives: number; mi: number; hours: number; days: number };

export type Review = {
  year: number;
  totals: YearTotals;
  previous: YearTotals;
  change: Record<keyof YearTotals, number | null>;
  new_roads: { count: number; mi: number };
  top_roads: { road_key: string; label: string; drive_count: number; mi: number }[];
  busiest_month: { month: string; drives: number; mi: number } | null;
  busiest_day: { date: string; drives: number; mi: number } | null;
  longest_drive: { drive_id: string; date: string; start: string; mi: number; minutes: number; status: string } | null;
  places: { area_id: string; kind: AreaKind; name: string; country: string | null; mi: number; drives: number }[] | null;
};

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

const qs = (p: Record<string, string | undefined>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v != null) as [string, string][]).toString();

export const api = {
  meta: () => get<Meta>("/api/meta"),
  roads: () => get<GeoJSON.FeatureCollection>("/api/roads"),
  traversals: async (): Promise<Traversals> => {
    const t = await get<{
      days: string[];
      drive_ids: string[];
      feature: number[];
      day: number[];
      drive: number[];
      length: number[];
      observed: number[];
    }>("/api/traversals");
    return {
      days: t.days,
      drive_ids: t.drive_ids,
      feature: Uint32Array.from(t.feature),
      day: Uint32Array.from(t.day),
      drive: Uint32Array.from(t.drive),
      length: Uint32Array.from(t.length),
      observed: Uint8Array.from(t.observed),
    };
  },
  road: (p: { way_id?: string; key?: string; since?: string; until?: string }) => get<Road>(`/api/road?${qs(p)}`),
  search: (q: string) => get<SearchResult>(`/api/search?${qs({ q })}`),
  area: (id: string) => get<Area>(`/api/area?${qs({ id })}`),
  areaShape: (id: string) => get<GeoJSON.Feature>(`/api/area/shape?${qs({ id })}`),
  areaRoads: (id: string) => get<GeoJSON.FeatureCollection>(`/api/area/roads?${qs({ id })}`),
  areaDay: (id: string, date: string) => get<GeoJSON.FeatureCollection>(`/api/area/day?${qs({ id, date })}`),
  patterns: (p: { road?: string; area?: string }) => get<Patterns>(`/api/patterns?${qs(p)}`),
  years: () => get<number[]>("/api/years"),
  review: (y: number) => get<Review>(`/api/review/${y}`),
  newRoads: (y: number) => get<GeoJSON.FeatureCollection>(`/api/review/${y}/new_roads`),
  day: (d: string) => get<DayDrive[]>(`/api/day/${d}`),
  drive: (id: string) => get<DriveDetail>(`/api/drive/${id}`),
  drives: (ids: string[]) => get<GeoJSON.FeatureCollection>(`/api/drives?${qs({ ids: ids.join(",") })}`),
};

export const fmtMi = (v: number | null | undefined, digits = 1) =>
  v == null ? "–" : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const fmtDate = (d: string) =>
  new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
