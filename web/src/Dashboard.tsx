import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fmtDate, fmtMi, type FacetKey, type FacetOption, type Filters, type QueryResult } from "./api";
import { niceMax, short, useTip } from "./Tip";

/** The dashboard: a shopping-site filter sidebar and the answer for the drives that match.
 * Within a section ticked options widen the results (OR); across sections they narrow (AND).
 * The filters live in the URL (?year=2024&dow=5,6), so a view can be bookmarked and Back works.
 * Compare splits them into two sets, A (blue) and B (orange), edited in the same sidebar. */

type Side = "a" | "b";
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
const COLOR = { a: "#2563eb", b: "#e0601a" };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TOD_OF_HOUR = (h: number) => (h < 6 ? "early" : h < 10 ? "morning" : h < 15 ? "midday" : h < 19 ? "afternoon" : "evening");

// ---------------------------------------------------------------- URL <-> filters

function readUrl(): { a: Filters; b: Filters; compare: boolean } {
  const p = new URLSearchParams(window.location.search);
  const one = (prefix: string): Filters => {
    const f: Filters = {};
    for (const k of KEYS) {
      const v = p.get(prefix + k);
      if (v) f[k] = v.split(",").filter(Boolean);
    }
    if (p.get(prefix + "from")) f.from = p.get(prefix + "from")!;
    if (p.get(prefix + "to")) f.to = p.get(prefix + "to")!;
    return f;
  };
  return { a: one(""), b: one("b_"), compare: p.get("compare") === "1" };
}

function writeUrl(a: Filters, b: Filters, compare: boolean, replace: boolean) {
  const p = new URLSearchParams();
  const put = (f: Filters, prefix: string) => {
    for (const k of KEYS) if (f[k]?.length) p.set(prefix + k, f[k]!.join(","));
    if (f.from) p.set(prefix + "from", f.from);
    if (f.to) p.set(prefix + "to", f.to);
  };
  put(a, "");
  if (compare) {
    p.set("compare", "1");
    put(b, "b_");
  }
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
const withoutSearch = (f: Filters): Filters => ({ ...f, search: undefined });

function toggle(f: Filters, key: FacetKey, id: string): Filters {
  const cur = f[key] ?? [];
  return { ...f, [key]: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] };
}
function add(f: Filters, key: FacetKey, id: string): Filters {
  const cur = f[key] ?? [];
  return cur.includes(id) ? f : { ...f, [key]: [...cur, id] };
}

/** A short name for a filter set ("Saturday, Sunday · 2024"), for compare headings. */
function nameOf(r: QueryResult | null, f: Filters): string {
  if (!r) return "…";
  const parts: string[] = [];
  const dows = f.dow ?? [];
  const byFacet = new Map<string, string[]>();
  for (const l of r.labels) byFacet.set(l.facet, [...(byFacet.get(l.facet) ?? []), l.label]);
  for (const k of KEYS) {
    let labs = byFacet.get(k);
    if (!labs) continue;
    if (k === "dow" && weekGroup(f)) labs = [weekGroup(f)!];
    parts.push(labs.map((x) => x.split(" · ")[0]).join(" or "));
  }
  if (f.from || f.to) parts.push(`${f.from ?? "start"} → ${f.to ?? "end"}`);
  return parts.length ? parts.join(" · ") : "All drives";
}

// ---------------------------------------------------------------- component

type Props = {
  quick: boolean;
  onOpenDrive: (id: string) => void;
  onOpenDay: (date: string) => void;
  onOpenYear: (year: number) => void;
  onShowMap: (filters: Filters, label: string) => void;
};

export default function Dashboard({ quick, onOpenDrive: openDriveRaw, onOpenDay: openDayRaw, onOpenYear, onShowMap }: Props) {
  // Quick mode has no matched roads, so single drives and days (road-by-road views) don't open yet.
  const onOpenDrive = (id: string) => { if (!quick) openDriveRaw(id); };
  const onOpenDay = (d: string) => { if (!quick) openDayRaw(d); };
  const init = useMemo(readUrl, []);
  const [fa, setFa] = useState<Filters>(init.a);
  const [fb, setFb] = useState<Filters>(init.b);
  const [compare, setCompare] = useState(init.compare);
  const [side, setSide] = useState<Side>("a");
  const [ra, setRa] = useState<QueryResult | null>(null);
  const [rb, setRb] = useState<QueryResult | null>(null);
  const [rows, setRows] = useState<QueryResult["drives"]>([]);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<Record<string, boolean>>({ year: true, dow: true, road: true, town: true });
  const [more, setMore] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  // Phones: results first, filters behind a button.
  const [showFilters, setShowFilters] = useState(() => window.innerWidth >= 700);
  const [tip, bind] = useTip();
  const seq = useRef(0);
  const firstWrite = useRef(true);

  const cur = side === "a" ? fa : fb;
  const setCur = useCallback((f: Filters) => (side === "a" ? setFa(f) : setFb(f)), [side]);

  // Filters -> URL (Back/Forward restore them).
  useEffect(() => {
    writeUrl(fa, fb, compare, firstWrite.current);
    firstWrite.current = false;
  }, [fa, fb, compare]);
  useEffect(() => {
    const onPop = () => {
      const u = readUrl();
      setFa(u.a);
      setFb(u.b);
      setCompare(u.compare);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Query: the side being edited gets facet counts (with its section searches); the other doesn't need them.
  useEffect(() => {
    const id = ++seq.current;
    setLoading(true);
    const h = setTimeout(async () => {
      const withS = (f: Filters, me: boolean) => (me ? { ...f, search } : withoutSearch(f));
      const [a, b] = await Promise.all([
        api.query(withS(fa, side === "a"), { facets: side === "a" || !compare, page_size: 25 }),
        compare ? api.query(withS(fb, side === "b"), { facets: side === "b", page_size: 25 }) : Promise.resolve(null),
      ]);
      if (id !== seq.current) return;
      setRa(a);
      setRb(b);
      setRows(a.drives);
      setPage(0);
      setLoading(false);
    }, 120);
    return () => clearTimeout(h);
  }, [fa, fb, compare, side, search]);

  const loadMore = async () => {
    const next = page + 1;
    const r = await api.query(withoutSearch(fa), { facets: false, page: next, page_size: 25 });
    setRows((x) => [...x, ...r.drives]);
    setPage(next);
  };

  const res = side === "a" ? ra : rb;
  const facets = res?.facets ?? (side === "a" ? ra?.facets : rb?.facets);

  // ------------------------------------------------------------ sidebar
  const sectionView = (s: Section) => {
    const fac = facets?.[s.key];
    const sel = cur[s.key] ?? [];
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
                  <button key={lab} onClick={() => setCur({ ...cur, [s.key]: ids })}>{lab}</button>
                ))}
              </div>
            )}
            <div className={`fopts${showAll && s.long ? " scroll" : ""}`}>
              {opts.map((o) => (
                <label key={o.id} className={`fopt${!o.selected && o.count === 0 ? " zero" : ""}`}>
                  <input type="checkbox" checked={o.selected} disabled={!o.selected && o.count === 0}
                    onChange={() => setCur(toggle(cur, s.key, o.id))} />
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

  // ------------------------------------------------------------ results pieces
  const approx = quick ? "≈ " : "";
  const tags = (r: QueryResult | null, f: Filters, set: (f: Filters) => void) => (
    <div className="tags">
      {(f.from || f.to) && (
        <span className="tag"><span className="muted">Dates:</span> <b>{f.from ?? "start"} → {f.to ?? "end"}</b>
          <button aria-label="Remove date range" onClick={() => set({ ...f, from: undefined, to: undefined })}>×</button></span>
      )}
      {weekGroup(f) && (
        <span className="tag"><span className="muted">Day of week:</span> <b>{weekGroup(f)}</b>
          <button aria-label={`Remove ${weekGroup(f)}`} onClick={() => set({ ...f, dow: [] })}>×</button></span>
      )}
      {r?.labels.filter((l) => !(l.facet === "dow" && weekGroup(f)))
        .map((l) => (
        <span className="tag" key={l.facet + l.id}><span className="muted">{TITLE[l.facet]}:</span> <b>{l.label}</b>
          <button aria-label={`Remove ${l.label}`} onClick={() => set(toggle(f, l.facet, l.id))}>×</button></span>
      ))}
    </div>
  );

  const onlyYear = (fa.year?.length === 1 && !compare) ? +fa.year[0] : null;

  const single = (r: QueryResult) => {
    const t = r.totals;
    const mmax = niceMax(Math.max(1, ...r.months.map((m) => m.mi)));
    const hmax = Math.max(1, ...r.grid.flat());
    const showYears = r.months.length > 18;
    return (
      <>
        <section className="dtiles">
          <div className="dtile"><div className="dt-l">Drives</div><div className="dt-v">{t.drives.toLocaleString()}</div>
            <div className="dt-s">{t.days.toLocaleString()} days</div></div>
          <div className="dtile"><div className="dt-l">Miles</div><div className="dt-v">{approx}{Math.round(t.mi).toLocaleString()}</div>
            <div className="dt-s">{t.on_road_mi != null ? `${Math.round(t.on_road_mi).toLocaleString()} mi on the road${(fa.road?.length ?? 0) > 1 ? "s" : ""} picked`
              : t.in_place_mi != null ? `${Math.round(t.in_place_mi).toLocaleString()} mi inside the place${"s"} picked`
              : t.drives ? `${(t.mi / t.drives).toFixed(1)} mi a drive` : ""}</div></div>
          <div className="dtile"><div className="dt-l">Hours</div><div className="dt-v">{Math.round(t.hours).toLocaleString()}</div>
            <div className="dt-s">{t.drives ? `${Math.round((t.hours * 60) / t.drives)} min a drive` : ""}</div></div>
          <div className="dtile"><div className="dt-l">Longest drive</div>
            {r.records.longest ? (
              <button className="dt-link" onClick={() => onOpenDrive(r.records.longest!.drive_id)}>
                <div className="dt-v">{approx}{fmtMi(r.records.longest.mi)} mi</div>
                <div className="dt-s">{fmtDate(r.records.longest.date)} · {r.records.longest.minutes} min</div>
              </button>) : <div className="dt-v">–</div>}
          </div>
        </section>

        <section className="drow">
          <div className="dcard grow">
            <div className="dcard-h"><h3>Miles per month</h3><span className="muted small">click a month to filter</span></div>
            <div className="vchart">
              <div className="vaxis" aria-hidden><span>{short(mmax)}</span><span>{short(mmax / 2)}</span><span>0</span></div>
              <div className="vbars-wrap">
                <div className="vbars">
                  <span className="vmid" />
                  {r.months.map((m) => (
                    <button key={m.month} className="vcol" aria-label={`${m.month}: ${Math.round(m.mi)} mi`}
                      {...bind(`${new Date(m.month + "-15").toLocaleDateString(undefined, { month: "long", year: "numeric" })} · ${approx}${Math.round(m.mi).toLocaleString()} mi · ${m.drives} drive${m.drives === 1 ? "" : "s"}`)}
                      onClick={() => setCur(add(add(cur, "year", m.month.slice(0, 4)), "month", String(+m.month.slice(5))))}>
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
          </div>
          <div className="dcard">
            <div className="dcard-h"><h3>When</h3><span className="muted small">drives · click to filter</span></div>
            <div className="dheat">
              <span />
              {Array.from({ length: 24 }, (_, h) => <span key={h} className="hh">{h % 6 === 0 ? ["12a", "6a", "12p", "6p"][h / 6] : ""}</span>)}
              {r.grid.map((row, d) => (
                <div key={d} className="hrow">
                  <span className="dd">{DAYS[d]}</span>
                  {row.map((v, h) => (
                    <button key={h} className="hcell" aria-label={`${DAYS[d]} ${h}:00, ${v} drives`}
                      style={{ background: v ? `color-mix(in oklab, var(--seq) ${Math.round(18 + 82 * Math.sqrt(v / hmax))}%, var(--bg))` : "var(--hover)" }}
                      {...bind(`${DAYS[d]} ${String(h).padStart(2, "0")}:00–${String(h).padStart(2, "0")}:59 · ${v} drive${v === 1 ? "" : "s"}`)}
                      onClick={() => setCur(add(add(cur, "dow", String(d)), "tod", TOD_OF_HOUR(h)))} />
                  ))}
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="drow three">
          {quick ? (
            <div className="dcard locked-card">
              <h3>Top roads <span aria-hidden>🔒︎</span></h3>
              <p className="muted">Which roads you drive most, and every date you drove each one, needs your drives matched to real roads.</p>
              <p className="muted small">Add road detail: step 4 of the guide (about an hour, once).</p>
            </div>
          ) : (
            <div className="dcard">
              <h3>Top roads <span className="muted small">by drives · click to filter</span></h3>
              <ol className="rank">
                {r.top_roads.map((x) => (
                  <li key={x.id}>
                    <div className="rank-row"><button className="linkish" onClick={() => setCur(add(cur, "road", x.id))}>{x.label}</button>
                      <span className="muted">{x.drives.toLocaleString()}</span></div>
                    <span className="rbar"><span style={{ width: `${(x.drives / (r.top_roads[0]?.drives || 1)) * 100}%` }} /></span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          <div className="dcard">
            <h3>Top towns <span className="muted small">by miles there · click to filter</span></h3>
            <ol className="rank">
              {r.top_towns.map((x) => (
                <li key={x.id}>
                  <div className="rank-row"><button className="linkish" onClick={() => setCur(add(cur, "town", x.id))}>{x.label}</button>
                    <span className="muted">{approx}{Math.round(x.mi).toLocaleString()} mi</span></div>
                  <span className="rbar blue"><span style={{ width: `${(x.mi / (r.top_towns[0]?.mi || 1)) * 100}%` }} /></span>
                </li>
              ))}
              {r.top_towns.length === 0 && <li className="muted">No towns for these drives.</li>}
            </ol>
          </div>
          <div className="dcard">
            <h3>Records</h3>
            <ul className="recs">
              {r.records.busiest_day && (
                <li><button className="linkish" onClick={() => onOpenDay(r.records.busiest_day!.date)}>
                  <span className="muted">Busiest day</span><b>{fmtDate(r.records.busiest_day.date)}</b>
                  <span className="muted">{approx}{fmtMi(r.records.busiest_day.mi, 0)} mi · {r.records.busiest_day.drives} drives</span></button></li>
              )}
              {r.records.longest && (
                <li><button className="linkish" onClick={() => onOpenDrive(r.records.longest!.drive_id)}>
                  <span className="muted">Longest drive</span><b>{fmtDate(r.records.longest.date)} {r.records.longest.start}</b>
                  <span className="muted">{approx}{fmtMi(r.records.longest.mi)} mi · {r.records.longest.minutes} min</span></button></li>
              )}
              {onlyYear != null && (
                <li><button className="linkish" onClick={() => onOpenYear(onlyYear)}><span className="muted">Year in review</span><b>{onlyYear} in review ›</b></button></li>
              )}
            </ul>
          </div>
        </section>

        <section className="dcard">
          <div className="dcard-h"><h3>Matching drives</h3><span className="muted small">newest first{quick ? "" : " · click to open"}</span></div>
          <div className="dtable-wrap">
            <table className="dtable">
              <thead><tr><th>Date</th><th>Start</th><th className="num">Miles</th><th className="num">Minutes</th><th>Towns</th></tr></thead>
              <tbody>
                {rows.map((d) => (
                  <tr key={d.drive_id} onClick={() => onOpenDrive(d.drive_id)} tabIndex={0}
                    onKeyDown={(e) => { if (e.key === "Enter") onOpenDrive(d.drive_id); }}>
                    <td>{fmtDate(d.date)}</td><td>{d.start}</td><td className="num">{approx}{fmtMi(d.mi)}</td>
                    <td className="num">{d.minutes}</td><td className="muted">{d.towns.join(", ")}</td>
                  </tr>
                ))}
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

  const pair = (a: QueryResult, b: QueryResult) => {
    const na = nameOf(a, fa);
    const nb = nameOf(b, fb);
    const per = (r: QueryResult) => (r.totals.drives ? r.totals.mi / r.totals.drives : 0);
    const pa = per(a);
    const pb = per(b);
    const diff = pa && pb ? Math.round((pb / pa - 1) * 100) : 0;
    const sentence = !a.totals.drives || !b.totals.drives ? "One side has no drives."
      : `B (${nb}): ${b.totals.drives.toLocaleString()} drives vs ${a.totals.drives.toLocaleString()} for A (${na}). ` +
        (Math.abs(diff) >= 5 ? `B's drives run ${Math.abs(diff)}% ${diff > 0 ? "longer" : "shorter"}: ${pb.toFixed(1)} vs ${pa.toFixed(1)} mi a drive.`
          : `Drives are about the same length: ${pb.toFixed(1)} vs ${pa.toFixed(1)} mi.`);
    const rowsT: [string, (r: QueryResult) => string][] = [
      ["Drives", (r) => r.totals.drives.toLocaleString()],
      ["Miles", (r) => approx + Math.round(r.totals.mi).toLocaleString()],
      ["Miles a drive", (r) => (r.totals.drives ? (r.totals.mi / r.totals.drives).toFixed(1) : "–")],
      ["Hours", (r) => Math.round(r.totals.hours).toLocaleString()],
      ["Days", (r) => r.totals.days.toLocaleString()],
    ];
    const months = a.months.map((m, i) => ({ month: m.month, a: m.mi, b: b.months[i]?.mi ?? 0 }));
    const mmax = niceMax(Math.max(1, ...months.flatMap((m) => [m.a, m.b])));
    const showYears = months.length > 18;
    const sideCard = (r: QueryResult, s: Side, name: string) => (
      <div className="dcard" key={s}>
        <h3><span className="sdot" style={{ background: COLOR[s] }} /> {s.toUpperCase()} · {name}</h3>
        <div className="cmp-cols">
          {!quick && (
            <div><div className="muted small">Top roads</div>
              <ol className="rank">{r.top_roads.slice(0, 5).map((x) => (
                <li key={x.id}><div className="rank-row"><span>{x.label}</span><span className="muted">{x.drives}</span></div>
                  <span className="rbar"><span style={{ width: `${(x.drives / (r.top_roads[0]?.drives || 1)) * 100}%`, background: COLOR[s] }} /></span></li>))}
              </ol></div>
          )}
          <div><div className="muted small">Top towns</div>
            <ol className="rank">{r.top_towns.slice(0, 5).map((x) => (
              <li key={x.id}><div className="rank-row"><span>{x.label.split(" · ")[0]}</span><span className="muted">{Math.round(x.mi)} mi</span></div>
                <span className="rbar"><span style={{ width: `${(x.mi / (r.top_towns[0]?.mi || 1)) * 100}%`, background: COLOR[s] }} /></span></li>))}
            </ol></div>
        </div>
        {r.records.longest && (
          <button className="linkish small" onClick={() => onOpenDrive(r.records.longest!.drive_id)}>
            Longest: <b>{approx}{fmtMi(r.records.longest.mi)} mi</b>, {fmtDate(r.records.longest.date)}</button>
        )}
        {r.records.busiest_day && <div className="small muted">Busiest day: {fmtDate(r.records.busiest_day.date)}, {approx}{fmtMi(r.records.busiest_day.mi, 0)} mi</div>}
      </div>
    );
    return (
      <>
        <p className="cmp-sentence">{sentence}</p>
        <section className="dcard">
          <table className="cmp-table">
            <thead><tr><th />
              <th><span className="sdot" style={{ background: COLOR.a }} /> A · {na}</th>
              <th><span className="sdot" style={{ background: COLOR.b }} /> B · {nb}</th></tr></thead>
            <tbody>{rowsT.map(([lab, fn]) => <tr key={lab}><th>{lab}</th><td>{fn(a)}</td><td>{fn(b)}</td></tr>)}</tbody>
          </table>
        </section>
        <section className="dcard">
          <div className="dcard-h"><h3>Miles per month</h3>
            <span className="legend2"><span><span className="sq" style={{ background: COLOR.a }} />A</span><span><span className="sq" style={{ background: COLOR.b }} />B</span></span></div>
          <div className="vchart">
            <div className="vaxis" aria-hidden><span>{short(mmax)}</span><span>{short(mmax / 2)}</span><span>0</span></div>
            <div className="vbars-wrap">
              <div className="vbars">
                <span className="vmid" />
                {months.map((m) => (
                  <div key={m.month} className="vcol pair" {...bind(`${m.month} · A ${Math.round(m.a)} mi · B ${Math.round(m.b)} mi`)}>
                    <span className="vbar" style={{ height: `${(m.a / mmax) * 100}%`, background: COLOR.a }} />
                    <span className="vbar" style={{ height: `${(m.b / mmax) * 100}%`, background: COLOR.b }} />
                  </div>
                ))}
              </div>
              <div className="vlabels">{months.map((m, i) => (
                <span key={m.month}>{showYears ? (m.month.endsWith("-01") || (i === 0 && +m.month.slice(5) <= 6) ? m.month.slice(0, 4) : "")
                  : new Date(m.month + "-15").toLocaleDateString(undefined, { month: "short" })}</span>))}</div>
            </div>
          </div>
        </section>
        <section className="drow">{sideCard(a, "a", na)}{sideCard(b, "b", nb)}</section>
      </>
    );
  };

  const total = ra?.totals;
  return (
    <div className={`dash${quick ? " quick" : ""}`}>
      <div className="dash-inner">
        <aside className="fside" aria-label="Filters" hidden={!showFilters}>
          <div className="fside-head">
            <h2>Filters</h2>
            {!isEmpty(cur) && <button className="linkbtn" onClick={() => setCur({})}>Clear all</button>}
          </div>
          {compare && (
            <div className="sidetabs" role="tablist">
              {(["a", "b"] as Side[]).map((s) => (
                <button key={s} role="tab" aria-selected={side === s} className={side === s ? "on" : ""} onClick={() => setSide(s)}>
                  <span className="sdot" style={{ background: COLOR[s] }} /> Editing {s.toUpperCase()}
                </button>
              ))}
            </div>
          )}
          {GROUPS.map((g) => (
            <div key={g.name} className="fgroup">
              <div className="fgroup-name">{g.name}</div>
              {g.dates && (
                <div className="fdates">
                  <label>From<input type="date" value={cur.from ?? ""} onChange={(e) => setCur({ ...cur, from: e.target.value || undefined })} /></label>
                  <label>To<input type="date" value={cur.to ?? ""} onChange={(e) => setCur({ ...cur, to: e.target.value || undefined })} /></label>
                </div>
              )}
              {g.sections.map(sectionView)}
            </div>
          ))}
        </aside>

        <main className="dmain">
          <div className="dhead">
            <div>
              {compare ? <h1>Compare</h1> : (
                <h1>{total ? `${total.drives.toLocaleString()} drive${total.drives === 1 ? "" : "s"} · ${approx}${Math.round(total.mi).toLocaleString()} mi` : "Loading…"}</h1>
              )}
              <div className="muted">{compare ? "Two sets of filters side by side: pick which one you're editing at the top of the sidebar."
                : isEmpty(fa) ? "All drives · tick filters on the left to narrow" : "matching your filters"}{loading && " · updating…"}</div>
            </div>
            <div className="dhead-actions">
              <button className="btn only-narrow" aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}>
                Filters{!isEmpty(cur) ? ` (${KEYS.reduce((n, k) => n + (cur[k]?.length ?? 0), 0) + (cur.from || cur.to ? 1 : 0)})` : ""}</button>
              <button className={`btn${compare ? " on" : ""}`} onClick={() => { setCompare(!compare); setSide("a"); }}>
                {compare ? "Stop comparing" : "Compare…"}</button>
              <button className="btn dark" onClick={() => onShowMap(withoutSearch(side === "b" && compare ? fb : fa), nameOf(side === "b" && compare ? rb : ra, side === "b" && compare ? fb : fa))}>
                Show on the map</button>
            </div>
          </div>
          {compare ? (
            <>
              <div className="cmp-tags">
                <div><span className="sdot" style={{ background: COLOR.a }} /> <b>A</b> {tags(ra, fa, setFa)}</div>
                <div><span className="sdot" style={{ background: COLOR.b }} /> <b>B</b> {tags(rb, fb, setFb)}</div>
              </div>
              {ra && rb && pair(ra, rb)}
            </>
          ) : (
            <>
              {!isEmpty(fa) && tags(ra, fa, setFa)}
              {ra && ra.totals.drives === 0 && <div className="dcard empty">No drives match these filters. Remove one to widen the search.</div>}
              {ra && ra.totals.drives > 0 && single(ra)}
            </>
          )}
          {quick && <p className="muted small">Quick mode: miles are measured along your GPS points (≈, a little low) until road detail is added.</p>}
        </main>
      </div>
      {tip}
    </div>
  );
}
