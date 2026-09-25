import { useEffect, useState } from "react";
import { api, fmtDate, fmtMi, type Review } from "./api";

type Props = {
  years: number[];
  year: number | null;
  setYear: (y: number) => void;
  onRoad: (key: string) => void;
  onDay: (date: string) => void;
  onDrive: (id: string, date: string, start: string) => void;
  onNewRoads: (year: number, count: number) => void;
  driveColor: (id: string) => string | undefined;
  newRoadsColor: string | undefined;
};

function Change({ v }: { v: number | null }) {
  if (v == null) return <span className="chg muted">–</span>;
  const pct = Math.round(v * 100);
  return <span className={`chg ${pct > 0 ? "up" : pct < 0 ? "down" : ""}`}>{pct > 0 ? "▲" : pct < 0 ? "▼" : ""}{Math.abs(pct)}%</span>;
}

export default function Year({ years, year, setYear, onRoad, onDay, onDrive, onNewRoads, driveColor, newRoadsColor }: Props) {
  const [r, setR] = useState<Review | null>(null);

  useEffect(() => {
    if (year == null) return;
    setR(null);
    api.review(year).then(setR);
  }, [year]);

  if (year == null) return <div className="muted">Loading…</div>;
  const i = years.indexOf(year);
  const t = r?.totals;
  const kinds = r?.places ? (["country", "state"] as const).map((k) => [k, r.places!.filter((p) => p.kind === k)] as const) : [];
  const ld = r?.longest_drive;
  const ldColor = ld ? driveColor(ld.drive_id) : undefined;

  return (
    <>
      <div className="daypick">
        <button disabled={i <= 0} onClick={() => setYear(years[i - 1])} title="Previous year">‹</button>
        <select value={year} onChange={(e) => setYear(+e.target.value)}>
          {years.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        <button disabled={i >= years.length - 1} onClick={() => setYear(years[i + 1])} title="Next year">›</button>
      </div>
      <h2>{year} in review</h2>
      {!r && <div className="muted">Loading…</div>}
      {r && t && (
        <>
          <dl className="stats two">
            <div><dt>Miles</dt><dd>{fmtMi(t.mi, 0)} <Change v={r.change.mi} /></dd></div>
            <div><dt>Drives</dt><dd>{t.drives.toLocaleString()} <Change v={r.change.drives} /></dd></div>
            <div><dt>Hours in the car</dt><dd>{Math.round(t.hours).toLocaleString()} <Change v={r.change.hours} /></dd></div>
            <div><dt>Days driving</dt><dd>{t.days.toLocaleString()} <Change v={r.change.days} /></dd></div>
          </dl>
          {r.previous.drives > 0 && <div className="muted small">
            vs {year - 1}: {fmtMi(r.previous.mi, 0)} mi · {r.previous.drives.toLocaleString()} drives ·{" "}
            {Math.round(r.previous.hours).toLocaleString()} h · {r.previous.days} days
          </div>}

          <h3>New roads</h3>
          <div className="newroads">
            <span><b>{r.new_roads.count.toLocaleString()}</b> roads · <b>{fmtMi(r.new_roads.mi)}</b> mi driven for the first time</span>
            <button className="chip" onClick={() => onNewRoads(year, r.new_roads.count)}
              style={newRoadsColor ? { background: newRoadsColor, borderColor: newRoadsColor, color: "#fff" } : undefined}>
              {newRoadsColor ? "on map" : "show on map"}
            </button>
          </div>

          <h3>Highlights</h3>
          <ul className="facts">
            {r.busiest_month && <li><span className="muted">Busiest month</span><span>{r.busiest_month.month} · {fmtMi(r.busiest_month.mi, 0)} mi, {r.busiest_month.drives} drives</span></li>}
            {r.busiest_day && (
              <li className="clickable" onClick={() => onDay(r.busiest_day!.date)} title="Open this day">
                <span className="muted">Busiest day</span><span>{fmtDate(r.busiest_day.date)} · {fmtMi(r.busiest_day.mi, 0)} mi ›</span>
              </li>
            )}
            {ld && (
              <li className="clickable" onClick={() => onDrive(ld.drive_id, ld.date, ld.start)} title="Add this drive to the map">
                <span className="muted">Longest drive</span>
                <span>{ldColor && <span className="dot" style={{ background: ldColor }} />} {fmtDate(ld.date)} {ld.start} · {fmtMi(ld.mi, 0)} mi, {ld.minutes} min</span>
              </li>
            )}
          </ul>

          {r.places && (
            <>
              <h3>Where</h3>
              {kinds.map(([k, ps]) => ps.length > 0 && (
                <div key={k} className="places">
                  <span className="muted">{k === "country" ? "Countries" : "States & provinces"}:</span>{" "}
                  {ps.map((p) => `${p.name} (${fmtMi(p.mi, 0)} mi)`).join(" · ")}
                </div>
              ))}
            </>
          )}

          <h3>Top 10 roads <span className="muted">(by drives)</span></h3>
          <ol className="toproads">
            {r.top_roads.map((x) => (
              <li key={x.road_key} onClick={() => onRoad(x.road_key)} title="Every time you drove this road">
                <span className="seg link">{x.label.replace(/ #\d+$/, "")}</span>
                <span className="muted">{x.drive_count.toLocaleString()} drives · {fmtMi(x.mi, 0)} mi</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </>
  );
}
