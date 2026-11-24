"""Activities Timeline labelled as driving that can't have been: flights and other impossible records.

An activity is set aside as "not a drive" only when no roads could be matched to it (so there's no road
evidence it was driven) and either

  * too fast: its distance needs more than MAX_AVG_MPH sustained over at least MIN_MINUTES, or
  * too far between points: two consecutive points are more than MAX_LEG_MI apart in a straight line.

Matched drives are never set aside, even when their timestamps make them look fast. Short drives
with minute-rounded times often "average" hundreds of mph, and they are real driving.

Set-aside activities move to `not_drives` (same columns as drives, plus `reason`). Their points, edges
and gaps are removed, and a later re-import of the same export skips them (drivelog.extract.store).
Nothing is lost: `not_drives` keeps the record.
"""

from __future__ import annotations

MAX_AVG_MPH = 90
MIN_MINUTES = 10
MAX_LEG_MI = 300

FIND = f"""
    WITH legs AS (
        SELECT drive_id, max(2 * 3958.8 * asin(sqrt(pow(sin(radians(lat - plat) / 2), 2) +
                   cos(radians(lat)) * cos(radians(plat)) * pow(sin(radians(lng - plng) / 2), 2)))) AS max_leg_mi
        FROM (SELECT drive_id, lat, lng, lag(lat) OVER w AS plat, lag(lng) OVER w AS plng
              FROM drive_points WINDOW w AS (PARTITION BY drive_id ORDER BY seq))
        WHERE plat IS NOT NULL GROUP BY 1)
    SELECT d.drive_id,
           CASE WHEN l.max_leg_mi > {MAX_LEG_MI}
                THEN 'points ' || round(l.max_leg_mi) || ' mi apart with no roads matched (a flight?)'
                ELSE round(d.reported_distance_m / 1609.344, 1) || ' mi in ' ||
                     datediff('minute', d.start_time, d.end_time) || ' min with no roads matched (over {MAX_AVG_MPH} mph)'
           END AS reason
    FROM drives d LEFT JOIN legs l USING (drive_id)
    WHERE d.match_status NOT IN ('ok', 'partial') AND (
        l.max_leg_mi > {MAX_LEG_MI}
        OR (datediff('second', d.start_time, d.end_time) >= {MIN_MINUTES * 60}
            AND d.reported_distance_m / 1609.344 / (datediff('second', d.start_time, d.end_time) / 3600.0) > {MAX_AVG_MPH}))
    ORDER BY d.start_time"""


def find(con) -> list[tuple[str, str]]:
    """[(drive_id, reason)] for activities that aren't drives."""
    return con.execute(FIND).fetchall()


def set_aside(con, found: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Move non-drives out of drives into not_drives. Returns what was moved."""
    import pyarrow as pa

    found = find(con) if found is None else found
    if not found:
        return []
    ids = [d for d, _ in found]
    con.execute("CREATE TABLE IF NOT EXISTS not_drives AS SELECT *, ''::VARCHAR AS reason FROM drives WHERE false")
    con.register("_nd", pa.table({"drive_id": ids, "reason": [r for _, r in found]}))
    con.execute("BEGIN")
    con.execute("INSERT INTO not_drives BY NAME SELECT d.*, n.reason FROM drives d JOIN _nd n USING (drive_id)")
    for t in ("drive_edges", "drive_gaps", "drive_points", "drives"):
        con.execute(f"DELETE FROM {t} WHERE drive_id IN (SELECT drive_id FROM _nd)")
    con.execute("COMMIT")
    con.unregister("_nd")
    return found


def run(cfg=None, dry_run: bool = False) -> list[tuple[str, str]]:
    from .config import load_config
    from .db import connect

    con = connect(cfg or load_config(), read_only=dry_run)
    out = find(con) if dry_run else set_aside(con)
    con.close()
    return out
