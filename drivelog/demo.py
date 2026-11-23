"""`drivelog demo`: try Drivelog without your own export.

Fetches the small Hawaii extract, builds routing tiles in a separate Valhalla container (port 8003),
loads a few weeks of synthetic Timeline data (demo/timeline_ios.json or demo/timeline_android.json),
and opens the viewer. Everything goes under data/demo/, apart from your own setup."""

from __future__ import annotations

import os
import subprocess

from .config import ROOT, Config, load_config

DEMO_CONFIG = "demo/config.toml"
TIMELINES = {"ios": "demo/timeline_ios.json", "android": "demo/timeline_android.json"}


def demo_config(fmt: str = "ios") -> Config:
    os.environ["DRIVELOG_CONFIG"] = DEMO_CONFIG  # the server and every stage read this config
    cfg = load_config()
    cfg.raw.setdefault("input", {})["timeline_path"] = TIMELINES[fmt]
    return cfg


def ensure_valhalla(cfg: Config) -> None:
    """Download + verify the extract and build tiles if needed, then start the container."""
    from .valhalla import _status_ok, compose, configure, fetch_extracts, merge_extracts, wait_ready, write_compose

    if _status_ok(cfg.valhalla["url"]):
        return
    if not (cfg.valhalla_dir / "merged.osm.pbf").exists():
        merge_extracts(fetch_extracts(cfg), cfg)
    write_compose(cfg)
    subprocess.run(compose(cfg, "up", "-d"), cwd=ROOT, check=True)
    print("waiting for the demo routing tiles (first run builds them: a few minutes)", flush=True)
    wait_ready(cfg, 3600, what="demo tile build")
    if configure(cfg):
        subprocess.run(compose(cfg, "restart"), cwd=ROOT, check=True)
        wait_ready(cfg, 600)


def run(fmt: str = "ios", serve: bool = True, port: int = 8765) -> None:
    from . import aggregate, areas, extract, fill, match, split

    cfg = demo_config(fmt)
    print("[1/5] routing tiles")
    ensure_valhalla(cfg)
    cfg.db_path.unlink(missing_ok=True)  # the demo always starts from the bundled data
    print(f"[2/5] extract ({fmt} export)")
    s = extract.run(cfg)
    print(f"      {s['drives']} drives")
    split.run(cfg, match_after=False, progress=False)  # stitched activities -> drives (none in the demo)
    print("[3/5] match + fill")
    print(f"      {match.run(cfg=cfg, progress=False)}")
    # Calibrate gap timing to the demo driver's pace (a first fill has nothing to calibrate against).
    fill.run(refill=True, cfg=cfg, progress=False)
    print("[4/5] aggregate + areas")
    aggregate.run(cfg)
    areas.build(cfg, progress=False)
    if serve:
        print("[5/5] viewer")
        import threading
        import webbrowser

        from .server import serve as run_server

        threading.Timer(3, webbrowser.open, [f"http://127.0.0.1:{port}"]).start()
        run_server(port=port)
