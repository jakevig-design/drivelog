import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import { api, fmtDate, fmtMi, ROUTE_COLORS, type FacetKey, type FacetOption, type Filters, type QueryResult, type SortKey } from "./api";
import { niceMax, short, useTip } from "./Tip";

/** The dashboard: a shopping-site filter sidebar, and for the drives that match, the totals, a month strip,
 * a map of the roads they used, and the drives themselves. Within a section ticked options widen the results
 * (OR); across sections they narrow (AND). The filters live in the URL (?year=2024&dow=5,6), so a view can be
 * bookmarked and Back works. The map is the app's one map, moved into this page (App.tsx). */

type Section = { key: FacetKey; title: string; long?: boolean; search?: string; shortcuts?: [string, string[]][]; roads?: boolean };
const GROUPS: { name: string; dates?: boolean; sections: Section[] }[] = [
  { name: "When", dates: true, sections: [
    { key: "year", title: "Year" },
    { key: "month", title: "Month" },
    { key: "dow", title: "Day of week", shortcuts: [["Weekdays", ["0", "1", "2", "3", "4"]], ["Weekends", ["5", "6"]]] },
    { key: "tod", title: "Time of day" },
  ] },
  { name: "Where", sections: [
    { key: "road", title: "Road", long: true, search: "Find a road", roads: true },
    { key: "town", title: "Town", long: true, search: "Find a town" },
    { key: "zip", title: "Zip code", long: true, search: "Find a zip code" },
    { key: "county", title: "County", long: true, search: "Find a county" },
    { key: "state", title: "State / province" },
    { key: "country", title: "Country" },
  ] },
  { name: "The drive", sections: [{ key: "len", title: "Length" }] },
];
const KEYS: FacetKey[] = ["year", "month", "dow", "tod", "road", "town", "zip", "county", "state", "country", "len"];
const TITLE: Record<string, string> = Object.fromEntries(GROUPS.flatMap((g) => g.sections.map((s) => [s.key, s.title])));
const SORT_NAME: Record<SortKey, [string, string]> = {
  date: ["newest first", "oldest first"], start: ["latest start first", "earliest start first"],
  mi: ["longest first", "shortest first"], minutes: ["longest time first", "shortest time first"],
};

// ---------------------------------------------------------------- URL <-> filters

function readUrl(): Filters {
  const p = new URLSearchParams(window.location.search);
  const f: Filters = {};
  for (const k of KEYS) {
    const v = p.get(k);
    if (v) f[k] = v.split(",").filter(Boolean);
  }
  if (p.get("from")) f.from = p.get("from")!;
  if (p.get("to")) f.to = p.get("to")!;
  return f;
}

function writeUrl(f: Filters, replace: boolean) {
  const p = new URLSearchParams();
  for (const k of KEYS) if (f[k]?.length) p.set(k, f[k]!.join(","));
  if (f.from) p.set("from", f.from);
  if (f.to) p.set("to", f.to);
  const qs = p.toString().replace(/%2C/g, ",").replace(/%3A/g, ":");
  const url = window.location.pathname + (qs ? "?" + qs : "");
  if (url === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
}

/** "Weekdays" / "Weekends" when exactly Mon–Fri or Sat–Sun are ticked (shown as one tag), else null. */
function weekGroup(f: Filters): string | null {
  const d = [...(f.dow ?? [])].sort().join(",");
  return d === "0,1,2,3,4" ? "Weekdays" : d === "5,6" ? "Weekends" : null;
}

const isEmpty = (f: Filters) => !f.from && !f.to && KEYS.every((k) => !f[k]?.length);
const filtersKey = (f: Filters) => JSON.stringify([KEYS.map((k) => [...(f[k] ?? [])].sort()), f.from ?? "", f.to ?? ""]);

function toggle(f: Filters, key: FacetKey, id: string): Filters {
  const cur = f[key] ?? [];
  return { ...f, [key]: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] };
}
function add(f: Filters, key: FacetKey, id: string): Filters {
  const cur = f[key] ?? [];
  return cur.includes(id) ? f : { ...f, [key]: [...cur, id] };
}

// ---------------------------------------------------------------- component

type Props = {
  quick: boolean;
  onOpenDrive: (id: string) => void;
  /** The drives matching the filters, for the map (null: no filters, every drive). */
  onMatch: (ids: string[] | null) => void;
  /** Drives highlighted on the map, in colour order. */
  selected: string[];
  onSelect: (ids: string[]) => void;
  /** Where the map goes on this page. */
  mapSlot: (el: HTMLDivElement | null) => void;
  /** Set by the dashboard: clicking a road on the map adds it as a filter. */
  addRoad: MutableRefObject<((key: string) => void) | null>;
};

export default function Dashboard({ quick, onOpenDrive, onMatch, selected, onSelect, mapSlot, addRoad }: Props) {
  const [f, setF] = useState<Filters>(readUrl);
  const [res, setRes] = useState<QueryResult | null>(null);
  const [rows, setRows] = useState<QueryResult["drives"]>([]);
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<SortKey>("date");
  const [desc, setDesc] = useState(true);
  const [open, setOpen] = useState<Record<string, boolean>>({ year: true, dow: true, road: true, town: true });
  const [more, setMore] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  // Phones: results first, filters behind a button.
  const [showFilters, setShowFilters] = useState(() => window.innerWidth >= 700);
  const [tip, bind] = useTip();
  const seq = useRef(0);
  const firstWrite = useRef(true);
  const key = useMemo(() => filtersKey(f), [f]);
  const firstKey = useRef(key);

  // Filters -> URL (Back/Forward restore them).
  useEffect(() => {
    writeUrl(f, firstWrite.current);
    firstWrite.current = false;
  }, [f]);
  useEffect(() => {
    const onPop = () => setF(readUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // A road clicked on the map becomes a filter.
  useEffect(() => {
    addRoad.current = (k: string) => setF((cur) => add(cur, "road", k));
    return () => { addRoad.current = null; };
  }, [addRoad]);

  // Results and facet counts (with the section searches).
  useEffect(() => {
    const id = ++seq.current;
    setLoading(true);
    const h = setTimeout(async () => {
      const r = await api.query({ ...f, search }, { facets: true, page_size: 25, sort, desc });
      if (id !== seq.current) return;
      setRes(r);
      setRows(r.drives);
      setPage(0);
      setLoading(false);
    }, 120);
    return () => clearTimeout(h);
  }, [f, search, sort, desc]);

  // The map follows the filters. A new filter set clears the drives picked from the old one.
  useEffect(() => {
    let live = true;
    if (key !== firstKey.current) {
      firstKey.current = "";
      onSelect([]);
    }
    if (isEmpty(f)) onMatch(null);
    else api.queryIds(f).then((ids) => { if (live) onMatch(ids); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const loadMore = async () => {
    const next = page + 1;
    const r = await api.query(f, { facets: false, page: next, page_size: 25, sort, desc });
    setRows((x) => [...x, ...r.drives]);
    setPage(next);
  };

  const sortBy = (k: SortKey) => {
    if (k === sort) setDesc(!desc);
    else {
      setSort(k);
      setDesc(true);
    }
  };

  // Click a drive: show it alone on the map (click again to clear). Shift, Cmd or Ctrl-click adds or removes it.
  const pick = (id: string, e: { shiftKey: boolean; metaKey: boolean; ctrlKey: boolean }) => {
    if (quick) return;
    if (e.shiftKey || e.metaKey || e.ctrlKey) onSelect(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
    else onSelect(selected.length === 1 && selected[0] === id ? [] : [id]);
  };
  const colorOf = (id: string) => {
    const i = selected.indexOf(id);
    return i < 0 ? undefined : ROUTE_COLORS[i % ROUTE_COLORS.length];
  };

  const facets = res?.facets;

  // ------------------------------------------------------------ sidebar
  const sectionView = (s: Section) => {
    const fac = facets?.[s.key];
    const sel = f[s.key] ?? [];
    const isOpen = open[s.key] || sel.length > 0;
    if (s.roads && quick) {
      return (
        <div className="fsec" key={s.key}>
          <div className="fsec-head locked"><span>{s.title}</span><span className="lock" aria-hidden>🔒︎</span></div>
          <div className="locked-note">Road filters come with road detail (step 4 of the guide).</div>
        </div>
      );
    }
    let opts: FacetOption[] = fac?.options ?? [];
    const showAll = more[s.key] || !!search[s.key];
    if (s.long && !showAll) opts = opts.slice(0, Math.max(6, sel.length));
    return (
      <div className="fsec" key={s.key}>
        <button className="fsec-head" aria-expanded={isOpen} onClick={() => setOpen({ ...open, [s.key]: !isOpen })}>
          <span>{s.title}{sel.length > 0 && <span className="nsel"> ({sel.length})</span>}</span>
          <span aria-hidden className="chev">{isOpen ? "−" : "+"}</span>
        </button>
        {isOpen && (
          <div className="fsec-body">
            {s.search && (
              <label className="fsearch">
                <span className="sr">{s.search}</span>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></svg>
                <input placeholder={s.search} value={search[s.key] ?? ""} onChange={(e) => setSearch({ ...search, [s.key]: e.target.value })} />
              </label>
            )}
            {s.shortcuts && (
              <div className="fshort">
                {s.shortcuts.map(([lab, ids]) => (
                  <button key={lab} onClick={() => setF({ ...f, [s.key]: ids })}>{lab}</button>
                ))}
              </div>
            )}
            <div className={`fopts${showAll && s.long ? " scroll" : ""}`}>
              {opts.map((o) => (
                <label key={o.id} className={`fopt${!o.selected && o.count === 0 ? " zero" : ""}`}>
                  <input type="checkbox" checked={o.selected} disabled={!o.selected && o.count === 0}
                    onChange={() => setF(toggle(f, s.key, o.id))} />
                  <span className="flab" title={o.label}>{o.label}</span>
                  <span className="fcount">{o.count.toLocaleString()}</span>
                </label>
              ))}
              {fac && opts.length === 0 && <div className="muted small">No matches</div>}
            </div>
            {s.long && !search[s.key] && (fac?.total ?? 0) > 6 && (
              <button className="linkbtn" onClick={() => setMore({ ...more, [s.key]: !more[s.key] })}>
                {more[s.key] ? "Show fewer" : `Show all ${Math.min(fac!.total, 100).toLocaleString()}${fac!.total > 100 ? "+" : ""}`}
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  // ------------------------------------------------------------ results
  const approx = quick ? "≈ " : "";
  const tags = (
    <div className="tags">
      {(f.from || f.to) && (
        <span className="tag"><span className="muted">Dates:</span> <b>{f.from ?? "start"} → {f.to ?? "end"}</b>
          <button aria-label="Remove date range" onClick={() => setF({ ...f, from: undefined, to: undefined })}>×</button></span>
      )}
      {weekGroup(f) && (
        <span className="tag"><span className="muted">Day of week:</span> <b>{weekGroup(f)}</b>
          <button aria-label={`Remove ${weekGroup(f)}`} onClick={() => setF({ ...f, dow: [] })}>×</button></span>
      )}
      {res?.labels.filter((l) => !(l.facet === "dow" && weekGroup(f))).map((l) => (
        <span className="tag" key={l.facet + l.id}><span className="muted">{TITLE[l.facet]}:</span> <b>{l.label}</b>
          <button aria-label={`Remove ${l.label}`} onClick={() => setF(toggle(f, l.facet, l.id))}>×</button></span>
      ))}
    </div>
  );

  const th = (k: SortKey, label: string, num = false) => (
    <th className={num ? "num" : undefined} aria-sort={sort === k ? (desc ? "descending" : "ascending") : "none"}>
      <button className={`sortbtn${sort === k ? " on" : ""}`} onClick={() => sortBy(k)} title={`Sort by ${label.toLowerCase()}`}>
        {label}<span className="arrow" aria-hidden>{sort === k ? (desc ? " ↓" : " ↑") : ""}</span>
      </button>
    </th>
  );

  const body = (r: QueryResult) => {
    const t = r.totals;
    const mmax = niceMax(Math.max(1, ...r.months.map((m) => m.mi)));
    const showYears = r.months.length > 18;
    const caption = quick
      ? "GPS points from your export, every drive. With road detail this map follows your filters."
      : selected.length
        ? `${selected.length} drive${selected.length === 1 ? "" : "s"} highlighted · shift-click more drives below to add them`
        : `Roads the ${isEmpty(f) ? "" : "matching "}drives used, darker = more often · click a road to filter by it · click a drive below to see its route`;
    return (
      <>
        <section className="dtiles">
          <div className="dtile"><div className="dt-l">Drives</div><div className="dt-v">{t.drives.toLocaleString()}</div>
            <div className="dt-s">{t.days.toLocaleString()} days</div></div>
          <div className="dtile"><div className="dt-l">Miles</div><div className="dt-v">{approx}{Math.round(t.mi).toLocaleString()}</div>
            <div className="dt-s">{t.on_road_mi != null ? `${Math.round(t.on_road_mi).toLocaleString()} mi on the road${(f.road?.length ?? 0) > 1 ? "s" : ""} picked`
              : t.in_place_mi != null ? `${Math.round(t.in_place_mi).toLocaleString()} mi inside the places picked`
              : t.drives ? `${(t.mi / t.drives).toFixed(1)} mi a drive` : ""}</div></div>
          <div className="dtile"><div className="dt-l">Hours</div><div className="dt-v">{Math.round(t.hours).toLocaleString()}</div>
            <div className="dt-s">{t.drives ? `${Math.round((t.hours * 60) / t.drives)} min a drive` : ""}</div></div>
          <div className="dtile"><div className="dt-l">Longest drive</div>
            {r.records.longest ? (
              <button className="dt-link" title={quick ? undefined : "Show it on the map"}
                onClick={() => pick(r.records.longest!.drive_id, { shiftKey: false, metaKey: false, ctrlKey: false })}>
                <div className="dt-v">{approx}{fmtMi(r.records.longest.mi)} mi</div>
                <div className="dt-s">{fmtDate(r.records.longest.date)} · {r.records.longest.minutes} min</div>
              </button>) : <div className="dt-v">–</div>}
          </div>
        </section>

        <section className="dcard strip">
          <div className="dcard-h"><h3>Miles per month</h3><span className="muted small">click a month to filter</span></div>
          <div className="vchart">
            <div className="vaxis" aria-hidden><span>{short(mmax)}</span><span>0</span></div>
            <div className="vbars-wrap">
              <div className="vbars">
                {r.months.map((m) => (
                  <button key={m.month} className="vcol" aria-label={`${m.month}: ${Math.round(m.mi)} mi`}
                    {...bind(`${new Date(m.month + "-15").toLocaleDateString(undefined, { month: "long", year: "numeric" })} · ${approx}${Math.round(m.mi).toLocaleString()} mi · ${m.drives} drive${m.drives === 1 ? "" : "s"}`)}
                    onClick={() => setF(add(add(f, "year", m.month.slice(0, 4)), "month", String(+m.month.slice(5))))}>
                    <span className="vbar" style={{ height: `${(m.mi / mmax) * 100}%` }} />
                  </button>
                ))}
              </div>
              <div className="vlabels">
                {r.months.map((m, i) => (
                  <span key={m.month}>{showYears ? (m.month.endsWith("-01") || (i === 0 && +m.month.slice(5) <= 6) ? m.month.slice(0, 4) : "")
                    : (i === 0 || m.month.endsWith("-01") ? m.month.slice(2, 4).replace(/^/, "’") + " " : "") + new Date(m.month + "-15").toLocaleDateString(undefined, { month: "short" }).slice(0, 3)}</span>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="dcard dmap-card">
          <div className="dcard-h"><h3>Map</h3>
            {selected.length > 0 && <button className="linkbtn" onClick={() => onSelect([])}>show all matching drives</button>}
          </div>
          <div className="dmap-slot" ref={mapSlot} />
          <div className="muted small dmap-cap">{caption}</div>
        </section>

        <section className="dcard">
          <div className="dcard-h"><h3>Matching drives</h3>
            <span className="muted small">{SORT_NAME[sort][desc ? 0 : 1]}{quick ? "" : " · click to show on the map · › opens the drive"}</span></div>
          <div className="dtable-wrap">
            <table className="dtable">
              <thead><tr>
                {th("date", "Date")}{th("start", "Start")}{th("mi", "Miles", true)}{th("minutes", "Minutes", true)}
                {!quick && <th>Longest on</th>}<th>Towns</th>{!quick && <th aria-label="Open" />}
              </tr></thead>
              <tbody>
                {rows.map((d) => {
                  const c = colorOf(d.drive_id);
                  return (
                    <tr key={d.drive_id} className={c ? "sel" : undefined} aria-selected={!!c} tabIndex={quick ? undefined : 0}
                      style={c ? { boxShadow: `inset 3px 0 0 ${c}` } : undefined}
                      onClick={(e) => pick(d.drive_id, e)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(d.drive_id, e); } }}>
                      <td>{c && <span className="sdot" style={{ background: c }} />} {fmtDate(d.date)}</td><td>{d.start}</td>
                      <td className="num">{approx}{fmtMi(d.mi)}</td><td className="num">{d.minutes}</td>
                      {!quick && <td className="road" title={d.top_road?.label}>{d.top_road
                        ? <>{d.top_road.label.split(" · ")[0]} <span className="muted">· {fmtMi(d.top_road.mi)} mi</span></> : <span className="muted">–</span>}</td>}
                      <td className="muted">{d.towns.join(", ")}</td>
                      {!quick && <td className="go"><button title="Open this drive: route, new roads, similar drives" aria-label="Open this drive"
                        onClick={(e) => { e.stopPropagation(); onOpenDrive(d.drive_id); }}>›</button></td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {rows.length < t.drives && (
            <button className="morebtn" onClick={loadMore}>Show more ({(t.drives - rows.length).toLocaleString()} left)</button>
          )}
        </section>
      </>
    );
  };

  const total = res?.totals;
  const nFilters = KEYS.reduce((n, k) => n + (f[k]?.length ?? 0), 0) + (f.from || f.to ? 1 : 0);
  return (
    <div className={`dash${quick ? " quick" : ""}`}>
      <div className="dash-inner">
        <aside className="fside" aria-label="Filters" hidden={!showFilters}>
          <div className="fside-head">
            <h2>Filters</h2>
            {!isEmpty(f) && <button className="linkbtn" onClick={() => setF({})}>Clear all</button>}
          </div>
          {GROUPS.map((g) => (
            <div key={g.name} className="fgroup">
              <div className="fgroup-name">{g.name}</div>
              {g.dates && (
                <div className="fdates">
                  <label>From<input type="date" value={f.from ?? ""} onChange={(e) => setF({ ...f, from: e.target.value || undefined })} /></label>
                  <label>To<input type="date" value={f.to ?? ""} onChange={(e) => setF({ ...f, to: e.target.value || undefined })} /></label>
                </div>
              )}
              {g.sections.map(sectionView)}
            </div>
          ))}
        </aside>

        <main className="dmain">
          <div className="dhead">
            <div>
              <h1 className="t-number-lg">{total ? `${total.drives.toLocaleString()} drive${total.drives === 1 ? "" : "s"} · ${approx}${Math.round(total.mi).toLocaleString()} mi` : "Loading…"}</h1>
              <div className="muted">{isEmpty(f) ? "All drives · tick filters on the left to narrow" : "matching your filters"}{loading && " · updating…"}</div>
            </div>
            <div className="dhead-actions">
              <button className="btn only-narrow" aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}>
                Filters{nFilters ? ` (${nFilters})` : ""}</button>
            </div>
          </div>
          {!isEmpty(f) && tags}
          {res && res.totals.drives === 0 && <div className="dcard empty">No drives match these filters. Remove one to widen the search.</div>}
          {/* The map's slot stays mounted when nothing matches, so the map isn't torn out and put back. */}
          {res && (res.totals.drives > 0 ? body(res) : <div hidden><div className="dmap-slot" ref={mapSlot} /></div>)}
          {quick && <p className="muted small">Quick mode: miles are measured along your GPS points (≈, a little low) until road detail is added.</p>}
        </main>
      </div>
      {tip}
    </div>
  );
}
