import { useEffect, useState } from "react";
import { api, fmtMi, type AreaHit, type Patterns as P, type SearchHit } from "./api";

/** What the patterns are filtered to: everything, one road, or one area. */
export type Scope = { kind: "all" } | { kind: "road"; key: string; label: string } | { kind: "area"; key: string; label: string };

type Unit = "drives" | "mi";

const fmt = (v: number, unit: Unit) => (unit === "mi" ? `${fmtMi(v)} mi` : `${v.toLocaleString()} drive${v === 1 ? "" : "s"}`);

/** One hue, light to dark, mixed over the panel background so it works in both themes. */
const shade = (v: number, max: number) =>
  v ? `color-mix(in oklab, var(--seq) ${Math.round(18 + 82 * Math.sqrt(v / max))}%, var(--bg))` : "var(--hover)";

export default function Patterns({ scope, setScope }: { scope: Scope; setScope: (s: Scope) => void }) {
  const [data, setData] = useState<P | null>(null);
  const [unit, setUnit] = useState<Unit>("drives");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ roads: SearchHit[]; areas: AreaHit[] } | null>(null);

  useEffect(() => {
    setData(null);
    api.patterns(scope.kind === "road" ? { road: scope.key } : scope.kind === "area" ? { area: scope.key } : {}).then(setData);
  }, [scope]);

  useEffect(() => {
    if (!q.trim()) return setHits(null);
    const h = setTimeout(() => api.search(q.trim()).then(setHits).catch(() => setHits(null)), 200);
    return () => clearTimeout(h);
  }, [q]);

  const pick = (s: Scope) => {
    setScope(s);
    setQ("");
    setHits(null);
  };

  const grid = data?.grid[unit];
  const gmax = grid ? Math.max(1, ...grid.flat()) : 1;
  const months = data?.months ?? [];
  const mmax = Math.max(1, ...months.map((m) => m[unit]));
  const lmax = Math.max(1, ...(data?.lengths.map((b) => b.drives) ?? [1]));

  return (
    <>
      <h2>Patterns</h2>
      <div className="sub muted">When you drive · local time of each drive</div>

      <div className="scope">
        <span className="scope-now">
          {scope.kind === "all" ? "All drives" : <><span className="muted">{scope.kind === "road" ? "Road" : "Area"}:</span> {scope.label}</>}
        </span>
        {scope.kind !== "all" && <button className="linkbtn" onClick={() => pick({ kind: "all" })}>all drives</button>}
      </div>
      <div className="search small">
        <input placeholder="Filter by road or area: 72, 96734, a town…" value={q} onChange={(e) => setQ(e.target.value)} />
        {hits && (
          <ul className="hits">
            {hits.roads.length + hits.areas.length === 0 && <li className="muted">No matches</li>}
            {hits.roads.slice(0, 5).map((h) => (
              <li key={h.road_key} onClick={() => pick({ kind: "road", key: h.road_key, label: h.label })}>
                <b>{h.label}</b><span className="count">road</span>
              </li>
            ))}
            {hits.areas.slice(0, 5).map((h) => (
              <li key={h.area_id} onClick={() => pick({ kind: "area", key: h.area_id, label: h.label })}>
                <b>{h.label}</b><span className="count">{h.kind}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="seg-toggle">
        <button className={unit === "drives" ? "on" : ""} onClick={() => setUnit("drives")}>Drives</button>
        <button className={unit === "mi" ? "on" : ""} onClick={() => setUnit("mi")}>Miles</button>
      </div>

      {!data && <div className="muted">Loading…</div>}
      {data && (
        <>
          <dl className="stats">
            <div><dt>Drives</dt><dd>{data.drives.toLocaleString()}</dd></div>
            <div><dt>Miles</dt><dd>{fmtMi(data.mi, 0)}</dd></div>
            <div><dt>Days</dt><dd>{data.distinct_days.toLocaleString()}</dd></div>
          </dl>

          <h3>Hour of day × day of week</h3>
          <div className="heat" role="table" aria-label={`${unit} by hour and weekday`}>
            <span />
            {Array.from({ length: 24 }, (_, h) => (
              <span key={h} className="hh muted">{h % 6 === 0 ? h : ""}</span>
            ))}
            {grid!.map((row, d) => (
              <div key={d} className="heat-row" role="row">
                <span className="dd muted">{data.days[d]}</span>
                {row.map((v, h) => (
                  <span key={h} className="cell" role="cell" style={{ background: shade(v, gmax) }}
                    title={`${data.days[d]} ${String(h).padStart(2, "0")}:00–${String(h).padStart(2, "0")}:59 · ${fmt(v, unit)}`} />
                ))}
              </div>
            ))}
          </div>
          <div className="heat-key muted">
            <span>fewer</span>
            <span className="ramp" />
            <span>more (max {fmt(gmax, unit)} in an hour)</span>
          </div>

          <h3>{unit === "mi" ? "Miles" : "Drives"} per month</h3>
          <div className="mbars" role="img" aria-label={`${unit} per month`}>
            {months.map((m, i) => (
              <div key={m.month} className="mcol" title={`${m.month} · ${fmt(m[unit], unit)}`}>
                <span className="mbar" style={{ height: `${(m[unit] / mmax) * 100}%` }} />
                {(i === 0 || m.month.endsWith("-01")) && <span className="mlab muted">{m.month.slice(0, 4)}</span>}
              </div>
            ))}
          </div>

          <h3>Drive length <span className="muted">(whole drive)</span></h3>
          <ul className="hbars">
            {data.lengths.map((b) => (
              <li key={b.label} title={`${b.label} mi · ${b.drives.toLocaleString()} drives`}>
                <span className="lab muted">{b.label} mi</span>
                <span className="track"><span className="fill" style={{ width: `${(b.drives / lmax) * 100}%` }} /></span>
                <span className="val">{b.drives.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
