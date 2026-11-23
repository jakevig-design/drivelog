"""Screenshots of the viewer and an audit of every network request it makes, in headless Chrome.

    uv run --with playwright --with pillow python scripts/screens.py OUT_DIR VIEWS.json [--base URL] [--gif NAME]

VIEWS.json is a list of {"name", "hash", "theme": "light"|"dark", "basemap": "map"|"satellite",
"click": [x, y] (optional), "wait_ms" (optional)}. Each view is a fresh page load of BASE#hash. Writes
OUT_DIR/<name>.png and OUT_DIR/network.json: every request host, how many requests went to it, and
whether any request off localhost carried anything but a basemap tile/style path (a POST body, a query
string, or a path that isn't a tile, style, font, sprite or TileJSON document).

--gif NAME also records the Explored animation: it opens #explore=<first month>, steps the month
slider with the arrow key and saves OUT_DIR/NAME.gif.

Uses the installed Google Chrome (channel "chrome"), so nothing extra is downloaded.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/inspect.py shadows the stdlib module when scripts/ is on sys.path (DEBUG.md, Phase 0).
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p and str(Path(p).resolve()) != _here]

import argparse
import json
import re
import time
from collections import Counter
from urllib.parse import urlparse

LOCAL = {"127.0.0.1", "localhost"}
# What a basemap request looks like: tile pyramids, style/TileJSON documents, glyph ranges, sprites.
BASEMAP_PATH = re.compile(
    r"^/(styles/[\w-]+|planet(/[\w.-]+)*(/\d+/\d+/\d+\.pbf)?|natural_earth.*|"
    r"ArcGIS/rest/services/World_Imagery/MapServer/tile/\d+/\d+/\d+|"
    r"fonts/.+\.pbf|sprites/.+|[\w/-]+/\d+/\d+/\d+\.(pbf|png|jpg|webp|mvt))$")

READY_JS = """() => {
  const m = window.__drivelogMap;
  const ready = performance.getEntriesByName('drivelog:ready').length > 0;
  return !!(m && ready && m.loaded() && m.areTilesLoaded() && !m.isMoving());
}"""


def wait_ready(page, timeout_s: float = 90, extra_ms: int = 1200) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if page.evaluate(READY_JS):
            break
        page.wait_for_timeout(300)
    page.wait_for_timeout(extra_ms)


def main() -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("views")
    ap.add_argument("--base", default="http://127.0.0.1:8765/")
    ap.add_argument("--gif")
    ap.add_argument("--gif-step", type=int, default=2, help="months per GIF frame")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    views = json.loads(Path(a.views).read_text())

    requests: list[dict] = []
    errors: list[str] = []

    def log(req):
        u = urlparse(req.url)
        requests.append({"host": u.hostname, "path": u.path, "query": u.query, "method": req.method,
                         "body": bool(req.post_data)})

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])

        def page_for(theme: str, basemap: str):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                      color_scheme=theme)
            ctx.add_init_script(f"localStorage.setItem('drivelog-theme', '{theme}');"
                                f"localStorage.setItem('drivelog-basemap', '{basemap}');")
            pg = ctx.new_page()
            pg.on("request", log)
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            return ctx, pg

        for v in views:
            if v.get("skip"):
                continue
            ctx, page = page_for(v.get("theme", "light"), v.get("basemap", "map"))
            page.goto(a.base + "#" + v.get("hash", ""), wait_until="domcontentloaded")
            wait_ready(page)
            if v.get("click"):
                page.mouse.click(*v["click"])
                page.wait_for_timeout(1500)
                wait_ready(page, extra_ms=600)
            if v.get("hover"):  # "selector" or "selector@index"
                sel, _, nth = v["hover"].partition("@")
                loc = page.locator(sel).nth(int(nth or 0))
                loc.wait_for(timeout=30_000)
                loc.hover()
                page.wait_for_timeout(300)
            page.wait_for_timeout(v.get("wait_ms", 0))
            page.screenshot(path=str(out / f"{v['name']}.png"))
            print("saved", v["name"], flush=True)
            ctx.close()

        if a.gif:
            from PIL import Image

            g = next((v for v in views if v.get("gif")), {"theme": "light", "basemap": "map"})
            ctx, page = page_for(g.get("theme", "light"), g.get("basemap", "map"))
            page.goto(a.base + "#explore=" + g.get("first", ""), wait_until="domcontentloaded")
            wait_ready(page)
            n = int(page.get_attribute("input.scrub", "max"))
            page.focus("input.scrub")
            page.keyboard.press("Home")
            frames = []
            for k in range(0, n + 1):
                if k % a.gif_step == 0 or k == n:
                    page.wait_for_timeout(250)
                    shot = out / "_frame.png"
                    page.screenshot(path=str(shot))
                    im = Image.open(shot).convert("RGB")
                    frames.append(im.resize((960, 600), Image.LANCZOS))
                page.keyboard.press("ArrowRight")
            (out / "_frame.png").unlink(missing_ok=True)
            pal = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f in frames]
            durations = [140] * (len(pal) - 1) + [2500]
            pal[0].save(out / f"{a.gif}.gif", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
            print("saved", a.gif, f"({len(frames)} frames)", flush=True)
            ctx.close()
        browser.close()

    hosts = Counter(r["host"] for r in requests)
    offsite = [r for r in requests if r["host"] not in LOCAL]
    suspect = [r for r in offsite if r["method"] != "GET" or r["body"] or r["query"] or not BASEMAP_PATH.match(r["path"])]
    report = {"console_errors": errors[:30], "hosts": dict(hosts.most_common()), "requests": len(requests), "offsite_requests": len(offsite),
              "offsite_not_basemap": suspect[:50], "offsite_path_shapes": sorted({r["host"] + re.sub(r"\d+", "N", r["path"]) for r in offsite})}
    (out / "network.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("hosts", "requests", "offsite_requests", "offsite_path_shapes")}, indent=1))
    print(f"off-localhost requests that aren't plain basemap GETs: {len(suspect)}")
    print(f"console errors: {len(errors)}" + "".join(f"\n  {e[:200]}" for e in errors[:10]))
    return 1 if suspect else 0


if __name__ == "__main__":
    raise SystemExit(main())
