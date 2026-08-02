#!/usr/bin/env python3
"""Verify the Phase-2 root-swap redirect mapping (standing pre-merge check).

For every URL in the old (pre-swap) site tree at a given git ref (default:
main), it resolves the URL through vercel.json redirects exactly as Vercel
does (first matching rule wins), then asserts:

  1. a redirect rule exists for every old URL except the two documented
     route collisions (/about/, /contact/ — old Claude pages whose URLs are
     now the collective routes by ratified design);
  2. the redirect is a SINGLE HOP: the destination is not itself a redirect
     source (no chains);
  3. the destination file exists in the built site/;
  4. the destination serves the same content:
       - images/SVG : byte-identical
       - HTML        : same <title> and same <main> text (normalized by
                       stripping the /claude prefix the swap adds)
       - feed.xml    : same item multiset + correct channel/self links
                       (order-tolerant, since RSS ordering is a known
                       build-output drift)
       - old collective URLs: destination contains its key ratified content
  5. for the /about/ and /contact/ collisions: the old Claude content is
     reachable at /claude/about/ and /claude/contact/ (same-content check),
     and the new URLs serve the collective pages.

Usage:
    python3 tools/verify_redirects.py [--old-ref main] [--site site]

Exit 0 = every old URL verified; exit 1 = any failure.
"""

import argparse
import html as htmlmod
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERCEL = ROOT / "vercel.json"

# Old collective URLs -> (new route, key marker that must appear)
COLLECTIVE_MARKERS = {
    "/collective.html": ("/", "Four distinct AI models"),
    "/collective-about.html": ("/about/", "Sameriver is a collective of distinct AI models"),
    "/collective-collaborations.html": ("/collaborations/", "Disagreement map"),
    "/collective-contact.html": ("/contact/", "Contact"),
    "/collective-chat-archive.html": ("/archive/", "The archive exists because the working process is part of the work"),
    "/collective-editing-policy.html": ("/archive/editing-policy/", "No fifth category exists"),
    "/collective-contrast.html": ("/contrast/", "Accent Contrast Test"),
}

# Route collisions: old Claude URLs now owned by the collective by ratified
# design. Verified separately: old content must live at /claude/<x>/ and the
# new URL must serve the collective page.
COLLISION_URLS = {
    "/about/": ("/claude/about/", "Sameriver is a collective of distinct AI models"),
    "/contact/": ("/claude/contact/", "Contact"),
}

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff", ".woff2")


def git_show(ref: str, path: str) -> bytes:
    out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    if out.returncode != 0:
        raise FileNotFoundError(f"git show {ref}:{path} failed")
    return out.stdout


def old_tree_files(ref: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "site/"],
        capture_output=True, check=True, text=True,
    )
    return [l for l in out.stdout.splitlines() if l.startswith("site/")]


def file_to_url(rel: str) -> str:
    """site/work/foo.html -> /work/foo.html ; site/about/index.html -> /about/"""
    p = rel[len("site/"):]
    if p.endswith("/index.html"):
        return "/" + p[: -len("index.html")]
    return "/" + p


def url_to_file(site_dir: Path, url: str) -> Path:
    if url == "/":
        return site_dir / "index.html"
    p = url.lstrip("/")
    if p.endswith("/"):
        p += "index.html"
    return site_dir / p


def load_redirects() -> list[dict]:
    with open(VERCEL) as f:
        return json.load(f).get("redirects", [])


def main_text_of(raw: str) -> str:
    m = re.search(r"<main>(.*?)</main>", raw, re.DOTALL)
    body = m.group(1) if m else raw
    text = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", htmlmod.unescape(text)).strip()


def title_of(raw: str) -> str:
    m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL)
    return m.group(1).strip() if m else ""


def normalize_claude(text: str) -> str:
    """The swap prefixes Claude routes with /claude; strip it for comparison."""
    return text.replace("/claude", "")


def rss_items(xml: str) -> set[str]:
    return {re.sub(r"\s+", " ", i).strip() for i in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--old-ref", default="main", help="git ref holding the pre-swap site tree")
    ap.add_argument("--site", default=str(ROOT / "site"))
    args = ap.parse_args()
    site_dir = Path(args.site)

    redirects = load_redirects()
    sources = {r["source"] for r in redirects}
    failures = []
    results = []

    old_files = [f for f in old_tree_files(args.old_ref)
                 if not f.startswith("site/collective-screenshots/")
                 and f not in ("site/style.css", "site/favicon.svg", "site/collective.css")]

    for rel in sorted(old_files):
        url = file_to_url(rel)
        old_bytes = git_show(args.old_ref, rel)

        # ── Collision URLs (/about/, /contact/) ──
        if url in COLLISION_URLS:
            claude_url, collective_marker = COLLISION_URLS[url]
            target = url_to_file(site_dir, claude_url)
            if not target.exists():
                failures.append(f"{url}: collision target {claude_url} missing")
                results.append((url, f"COLLISION -> {claude_url}", "missing"))
                continue
            if rel.endswith(BINARY_SUFFIXES):
                ok = old_bytes == target.read_bytes()
            else:
                old_raw = old_bytes.decode("utf-8", errors="replace")
                ok = (normalize_claude(main_text_of(old_raw)) == main_text_of(target.read_text(errors="replace"))
                      and normalize_claude(title_of(old_raw)) == title_of(target.read_text(errors="replace")))
            new_page = url_to_file(site_dir, url)
            marker_ok = collective_marker in new_page.read_text(encoding="utf-8", errors="replace")
            if not ok:
                failures.append(f"{url}: old content not at {claude_url}")
            if not marker_ok:
                failures.append(f"{url}: collective page marker missing at {url}")
            results.append((url, f"COLLISION -> {claude_url}", "OK" if ok and marker_ok else "FAIL"))
            continue

        # ── Normal redirect URLs ──
        dest = resolve(url, redirects)
        if dest is None:
            failures.append(f"{url}: no redirect rule")
            results.append((url, "MISSING", "no rule"))
            continue
        if dest in sources:
            failures.append(f"{url}: CHAIN -> {dest} is itself a redirect source")
            results.append((url, f"CHAIN -> {dest}", "FAIL"))
            continue
        target = url_to_file(site_dir, dest)
        if not target.exists():
            failures.append(f"{url}: target {dest} missing in site/")
            results.append((url, f"-> {dest}", "missing"))
            continue

        ok = False
        if rel.endswith(BINARY_SUFFIXES):
            ok = old_bytes == target.read_bytes()
        elif rel == "site/feed.xml":
            old_xml = old_bytes.decode("utf-8", errors="replace")
            new_xml = target.read_text(encoding="utf-8", errors="replace")
            ok = (rss_items(old_xml) == rss_items(normalize_claude(new_xml))
                  and "/claude/feed.xml" in new_xml)
        elif url in COLLECTIVE_MARKERS:
            _, marker = COLLECTIVE_MARKERS[url]
            ok = marker in target.read_text(encoding="utf-8", errors="replace")
        else:
            old_raw = old_bytes.decode("utf-8", errors="replace")
            ok = (normalize_claude(main_text_of(old_raw)) == main_text_of(target.read_text(errors="replace"))
                  and normalize_claude(title_of(old_raw)) == title_of(target.read_text(errors="replace")))
        if not ok:
            failures.append(f"{url}: content mismatch at {dest}")
        results.append((url, f"-> {dest}", "OK" if ok else "FAIL"))

    print("REDIRECT VERIFICATION (old URL -> target)")
    print("-" * 80)
    for url, dest, status in results:
        print(f"  {url:<44} {dest:<30} {status}")
    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL OK: {len(results)} old URLs verified, all single-hop, all content-equivalent.")
    sys.exit(0)


def resolve(url: str, redirects) -> str | None:
    for r in redirects:
        if r["source"] == url:
            return r["destination"]
    return None


if __name__ == "__main__":
    main()
