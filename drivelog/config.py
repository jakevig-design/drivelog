"""Load config.toml from the project root."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Config:
    raw: dict

    def _get(self, section, key, default=None):
        return self.raw.get(section, {}).get(key, default)

    @property
    def timeline_path(self) -> Path:
        return ROOT / self._get("input", "timeline_path", "Timeline.json")

    @property
    def fallback_tz(self) -> str:
        return self._get("time", "fallback_tz", "America/New_York")

    @property
    def db_path(self) -> Path:
        override = os.environ.get("DRIVELOG_DB")  # e.g. a scratch copy while a long run holds the real DB
        return Path(override) if override else ROOT / self._get("storage", "db_path", "data/drivelog.duckdb")

    @property
    def drive_types(self) -> list[str]:
        return self._get("extract", "drive_types", ["in passenger vehicle", "motorcycling"])

    @property
    def min_points(self) -> int:
        return self._get("extract", "min_points", 2)

    @property
    def min_distance_m(self) -> float:
        return self._get("extract", "min_distance_m", 500)

    @property
    def stationary_m(self) -> float:
        """A drive's moving window starts once it is this far from its first point (and ends once it is
        within this distance of its last). Minutes parked at either end don't count toward route timing."""
        return self._get("fill", "stationary_m", 100)

    @property
    def valhalla(self) -> dict:
        return self.raw.get("valhalla", {})

    @property
    def valhalla_dir(self) -> Path:
        """Extracts, merged PBF and routing tiles (mounted into the container as /custom_files)."""
        return ROOT / self.valhalla.get("data_dir", "data/valhalla")

    @property
    def compose_path(self) -> Path:
        return ROOT / self.valhalla.get("compose_file", "docker-compose.yml")

    @property
    def container(self) -> str:
        return self.valhalla.get("container", "drivelog-valhalla")

    @property
    def port(self) -> int:
        from urllib.parse import urlparse

        return urlparse(self.valhalla.get("url", "http://localhost:8002")).port or 8002


def config_path() -> Path:
    """The file named by DRIVELOG_CONFIG (relative to the project root), else config.toml, else the
    shipped config.example.toml (so a fresh clone runs with the defaults)."""
    if "DRIVELOG_CONFIG" in os.environ:
        return ROOT / os.environ["DRIVELOG_CONFIG"]
    own = ROOT / "config.toml"
    return own if own.exists() else ROOT / "config.example.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    with open(path, "rb") as f:
        return Config(tomllib.load(f))
