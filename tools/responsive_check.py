#!/usr/bin/env python3
"""Responsive pre-merge check for the sameriver collective pages.

Standing check (ratified 2026-08-02 after the Phase 1 responsive incident):
every collective page x every viewport in the four-viewport matrix must show
0px horizontal overflow, and every waffle-panel link must be reachable.

Viewports (exactly): 1440x900, 768x1024, 390x844, 320x568.

What it measures, per page and per width:
  * horizontal overflow = document.documentElement.scrollWidth - innerWidth
    (must be <= 0; reported as max(0, value) and failed if > 0)
  * waffle panel bounding box (must be horizontally inside the viewport)
  * every .waffle-link bounding box, after scrolling the panel into view
    (must be fully inside the viewport both axes — i.e. reachable/clickable)

Screenshots: full-page PNG at 390 and 320 for every page, written to
--outdir (default src/collective/screenshots/responsive/).

Usage:
    python3 tools/responsive_check.py [--base URL] [--build] [--outdir DIR]

    --base      base URL of a running site/ server (default: starts its own
                local server on site/ at a free port)
    --build     run `python3 tools/build.py` first (so the check exercises
                the freshly built site/)
    --outdir    screenshots directory (default: src/collective/screenshots/responsive)

Requirements:
    * Playwright for Python:  pip install playwright
    * a Chrome/Chromium binary — resolution order:
        1. $CHROME_PATH
        2. macOS default  /Applications/Google Chrome.app/.../Google Chrome
        3. Linux common paths (google-chrome, chromium, chromium-browser)
        4. Playwright's bundled chromium (if `playwright install` was run)

Exit code 0 = whole matrix clean; 1 = any overflow or unreachable link.
"""

import argparse
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

PAGES = [
    "collective.html",
    "collective-about.html",
    "collective-collaborations.html",
    "collective-contact.html",
    "collective-chat-archive.html",
    "collective-editing-policy.html",
    "collective-contrast.html",
]

VIEWPORTS = [
    (1440, 900),
    (768, 1024),
    (390, 844),
    (320, 568),
]

SCREENSHOT_WIDTHS = (390, 320)  # task: screenshot each page at 390 and 320

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_chrome():
    candidates = []
    env = os.environ.get("CHROME_PATH")
    if env:
        candidates.append(env)
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None  # fall back to Playwright's bundled chromium


class QuietServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(site_dir: Path):
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(site_dir), **kw)

        def log_message(self, *args):
            pass

    server = QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=None, help="base URL of a running server")
    ap.add_argument("--build", action="store_true", help="run tools/build.py first")
    ap.add_argument("--outdir", default=str(REPO_ROOT / "src/collective/screenshots/responsive"))
    args = ap.parse_args()

    if args.build:
        subprocess.run([sys.executable, str(REPO_ROOT / "tools/build.py")], check=True)

    chrome = find_chrome()
    launch_kwargs = {"headless": True}
    if chrome:
        launch_kwargs["executable_path"] = chrome

    server = None
    if args.base:
        base = args.base.rstrip("/")
    else:
        server, port = start_server(REPO_ROOT / "site")
        base = f"http://127.0.0.1:{port}"
        print(f"[check] serving site/ at {base} (shuts down when the run finishes)")

    from playwright.sync_api import sync_playwright

    failures = []
    rows = []  # (page, width, height, overflow_px, panel_box, links_ok)

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        for width, height in VIEWPORTS:
            for page in PAGES:
                url = f"{base}/{page}"
                pg = browser.new_page(viewport={"width": width, "height": height})
                pg.goto(url, wait_until="networkidle")

                overflow = pg.evaluate(
                    "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)"
                )

                # open the waffle panel and measure it
                panel_box = pg.evaluate(
                    """() => {
                        const details = document.querySelector('.waffle-menu');
                        if (!details) return null;
                        details.open = true;
                        const r = document.querySelector('.waffle-panel').getBoundingClientRect();
                        return {x: Math.round(r.x), y: Math.round(r.y),
                                width: Math.round(r.width), height: Math.round(r.height)};
                    }"""
                )
                # scroll the panel into view, then check every link is reachable
                links_result = pg.evaluate(
                    """() => {
                        const panel = document.querySelector('.waffle-panel');
                        panel.scrollIntoView({block: 'nearest'});
                        const w = window.innerWidth, h = window.innerHeight;
                        const bad = [];
                        for (const a of document.querySelectorAll('.waffle-link')) {
                            const r = a.getBoundingClientRect();
                            if (r.x < 0 || r.x + r.width > w || r.y < 0 || r.y + r.height > h)
                                bad.push(a.textContent.trim());
                        }
                        return {ok: bad.length === 0, bad};
                    }"""
                )

                links_ok = links_result["ok"]
                links_bad = links_result["bad"]

                # screenshots at 390 and 320
                if width in SCREENSHOT_WIDTHS:
                    out = Path(args.outdir)
                    out.mkdir(parents=True, exist_ok=True)
                    fname = out / f"{Path(page).stem}-{width}.png"
                    pg.screenshot(path=str(fname), full_page=True)

                rows.append(
                    (page, width, height, overflow, panel_box, links_ok, links_bad)
                )
                if overflow > 0:
                    failures.append(f"{page} @ {width}x{height}: overflow {overflow}px")
                if not links_ok:
                    failures.append(
                        f"{page} @ {width}x{height}: waffle links off-viewport: {links_bad}"
                    )
                pg.close()
        browser.close()

    if server:
        server.shutdown()

    # ── report ──
    print()
    print("RESPONSIVE MATRIX (overflow px = document.scrollWidth - innerWidth; 0 required)")
    print(f"{'page':<32} {'1440x900':>9} {'768x1024':>9} {'390x844':>9} {'320x568':>9}  waffle@320")
    print("-" * 92)
    for page in PAGES:
        vals = {}
        for r in rows:
            if r[0] == page:
                vals[r[1]] = r
        line = f"{page:<32}"
        for w, _h in VIEWPORTS:
            ov = vals[w][3]
            line += f" {ov:>7}px"
        link_cell = "OK" if vals[320][5] else f"FAIL {vals[320][6]}"
        line += f"  {link_cell}"
        print(line)
    print()
    print("WAFFLE PANEL BOX (x, y, w, h) — must be horizontally inside viewport")
    for w, _h in VIEWPORTS:
        boxes = [f"{r[0]}:{r[4]}" for r in rows if r[1] == w and r[4]]
        print(f"  {w}x{_h}: " + "  ".join(boxes))

    # JSON summary for scripting / CI
    summary = {
        "viewports": [list(v) for v in VIEWPORTS],
        "pages": PAGES,
        "measurements": [
            {
                "page": r[0],
                "width": r[1],
                "height": r[2],
                "overflow_px": r[3],
                "panel": r[4],
                "links_reachable": r[5],
            }
            for r in rows
        ],
        "failures": failures,
    }
    print()
    print("SUMMARY_JSON " + json.dumps(summary))

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nALL CLEAN: every page x every width at 0px horizontal overflow; waffle links reachable.")
    sys.exit(0)


if __name__ == "__main__":
    main()
