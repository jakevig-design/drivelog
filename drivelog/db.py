"""DuckDB connection and schema."""

from __future__ import annotations

import duckdb

from .config import Config, load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    drive_id VARCHAR PRIMARY KEY,
    start_time TIMESTAMP NOT NULL,      -- UTC
    end_time TIMESTAMP NOT NULL,        -- UTC
    local_date DATE NOT NULL,
    local_start TIMESTAMP NOT NULL,     -- local wall time
    utc_offset_min INTEGER,
    reported_distance_m DOUBLE,
    activity_confidence DOUBLE,
    n_points INTEGER,
    match_status VARCHAR DEFAULT 'pending',  -- pending, ok, partial, failed, out_of_coverage
    match_score DOUBLE,
    matched_length_m DOUBLE,
    match_detail VARCHAR,
    moving_start TIMESTAMP,             -- UTC; start_time minus parked lead-in (drivelog.extract.moving_window)
    moving_end TIMESTAMP                -- UTC; end_time minus parked tail
);

CREATE TABLE IF NOT EXISTS drive_points (
    drive_id VARCHAR NOT NULL,
    seq INTEGER NOT NULL,
    ts TIMESTAMP NOT NULL,              -- UTC
    lat DOUBLE NOT NULL,
    lng DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_edges (
    drive_id VARCHAR NOT NULL,
    seq INTEGER NOT NULL,
    way_id BIGINT,
    name VARCHAR,
    ref VARCHAR,
    road_class VARCHAR,
    length_m DOUBLE,
    geometry VARCHAR,                   -- JSON [[lng, lat], ...]
    inferred BOOLEAN DEFAULT false,     -- true = routed across a gap between observed points
    confidence VARCHAR DEFAULT 'observed'  -- observed | high | low
);

-- Gaps between observed segments of a drive, and how each was filled (drivelog.fill).
CREATE TABLE IF NOT EXISTS drive_gaps (
    drive_id VARCHAR NOT NULL,
    seq INTEGER NOT NULL,               -- fill edges use seq + 0..4999
    a_lng DOUBLE, a_lat DOUBLE, a_ts TIMESTAMP,   -- ts is NULL when not known
    b_lng DOUBLE, b_lat DOUBLE, b_ts TIMESTAMP,
    straight_m DOUBLE,
    filled BOOLEAN DEFAULT false,
    route_m DOUBLE,
    route_s DOUBLE,
    elapsed_s DOUBLE,
    candidates INTEGER,
    habit_share DOUBLE,
    confidence VARCHAR
);
"""

# Columns added after the first release; ALTER keeps existing databases working.
MIGRATIONS = [
    "ALTER TABLE drive_edges ADD COLUMN IF NOT EXISTS inferred BOOLEAN DEFAULT false",
    "ALTER TABLE drive_edges ADD COLUMN IF NOT EXISTS confidence VARCHAR DEFAULT 'observed'",
    "ALTER TABLE drives ADD COLUMN IF NOT EXISTS moving_start TIMESTAMP",
    "ALTER TABLE drives ADD COLUMN IF NOT EXISTS moving_end TIMESTAMP",
]


def connect(cfg: Config | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    cfg = cfg or load_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(cfg.db_path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
        for m in MIGRATIONS:
            con.execute(m)
    return con
