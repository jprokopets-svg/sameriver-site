#!/usr/bin/env python3
"""
build.py — sameriver static site builder.

Converts markdown content in src/content/ to HTML in site/ using
Jinja-style templates in src/templates/. Also generates RSS feed,
section index pages, and a changelog page.

Usage:
    python build.py          # build everything
    python build.py --watch  # (future) rebuild on changes
"""

from __future__ import annotations

import io
import os
import re
import sys
import json
import html
import email.utils
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

try:
    import markdown
except ImportError:
    print("ERROR: 'markdown' package required. Install with: pip install markdown", file=sys.stderr)
    sys.exit(1)


# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "src" / "content"
TEMPLATES = ROOT / "src" / "templates"
SITE = ROOT / "site"
CHANGELOG = ROOT / "CHANGELOG.md"
BASE_URL = "https://sameriver.example.com"  # placeholder

SECTION_ORDER = ["work", "notes", "log", "about", "influences", "contact"]
SECTION_TITLES = {
    "work": "Work",
    "notes": "Notes",
    "log": "Log",
    "about": "About",
    "influences": "Influences",
    "contact": "Contact",
}
SECTION_DESCRIPTIONS = {
    "work": "Long-form research and writing.",
    "notes": "Shorter thoughts, observations, and marginalia.",
    "log": "Brief, dated updates on ongoing work.",
    "about": "About this site and its author.",
    "influences": "People, ideas, and works that shape this project.",
    "contact": "How to reach me.",
}
# Sections that appear in RSS feed
RSS_SECTIONS = {"work", "notes", "log"}


# ── Frontmatter parser ─────────────────────────────────────────────
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter. Returns (metadata, body)."""
    text = text.lstrip("\ufeff")  # strip BOM
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_lines = text[3:end].strip().split("\n")
    body = text[end + 4:].strip()

    metadata = {}
    for line in fm_lines:
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip().strip('"').strip("'")
            metadata[key] = val

    return metadata, body


# ── Template engine ────────────────────────────────────────────────
class Template:
    """Minimal template engine: variable substitution + block inheritance."""

    def __init__(self, text: str):
        self.text = text

    def render(self, **vars) -> str:
        result = self.text

        # Split/extend blocks: {% block name %}...{% endblock %}
        # We handle inheritance by replacing blocks in the child with blocks from the base
        # For simplicity: process {% extends "..." %} first, then substitute {{ var }}

        # Extends
        extends_match = re.search(r'\{%\s*extends\s+"([^"]+)"\s*%\}', result)
        if extends_match:
            base_name = extends_match.group(1)
            base_path = TEMPLATES / base_name
            if not base_path.exists():
                print(f"  WARNING: template '{base_name}' not found", file=sys.stderr)
            else:
                base_text = base_path.read_text(encoding="utf-8")

                # Extract blocks from child
                child_blocks = {}
                for m in re.finditer(
                    r'\{%\s*block\s+(\w+)\s*%\}(.*?)\{%\s*endblock\s*%\}',
                    result, re.DOTALL
                ):
                    child_blocks[m.group(1)] = m.group(2)

                # Substitute blocks in base with child versions
                def replace_block(m):
                    name = m.group(1)
                    if name in child_blocks:
                        # Render the child block content (which may have further template vars)
                        child_content = child_blocks[name]
                        # Recursively render templates in child content
                        child_content = self._render_vars(child_content, vars)
                        return child_content
                    return m.group(0)

                # Remove the extends tag
                base_text = re.sub(r'\{%\s*extends\s+"[^"]+"\s*%\}', '', base_text)

                # Replace blocks in base
                base_text = re.sub(
                    r'\{%\s*block\s+(\w+)\s*%\}(.*?)\{%\s*endblock\s*%\}',
                    replace_block, base_text, flags=re.DOTALL
                )

                result = base_text

        # Remove any remaining template tags
        result = re.sub(r'\{%\s*(end)?block\s+\w*\s*%\}', '', result)

        # {{ var }} substitution
        result = self._render_vars(result, vars)

        return result

    def _render_vars(self, text: str, vars: dict) -> str:
        def repl_var(m):
            key = m.group(1).strip()
            # Support dotted paths: entry.title
            parts = key.split(".")
            val = vars
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part, "")
                else:
                    # Try as attribute
                    try:
                        val = getattr(val, part, "")
                    except Exception:
                        val = ""
            return str(val) if val is not None else ""

        # For %}
        text = re.sub(r'\{%\s*for\s+(\w+)\s+in\s+(\w+(?:\.\w+)*)\s*%\}',
                      '<!-- for loop -->', text)
        text = re.sub(r'\{%\s*endfor\s*%\}', '<!-- endfor -->', text)
        text = re.sub(r'\{%\s*if\s+(.*?)\s*%\}', '<!-- if -->', text)
        text = re.sub(r'\{%\s*endif\s*%\}', '<!-- endif -->', text)
        text = re.sub(r'\{%\s*else\s*%\}', '<!-- else -->', text)

        return re.sub(r'\{\{\s*([^}]+)\s*\}\}', repl_var, text)


# ── Build functions ────────────────────────────────────────────────
def load_template(name: str) -> Template:
    path = TEMPLATES / name
    if not path.exists():
        print(f"  WARNING: template '{name}' not found", file=sys.stderr)
        return Template("{{ content }}")
    return Template(path.read_text(encoding="utf-8"))


def md_to_html(text: str) -> str:
    return markdown.markdown(text, extensions=["fenced_code", "codehilite"])


def format_date(iso_str: str) -> str:
    """Format YYYY-MM-DD to human-readable."""
    if not iso_str:
        return ""
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return d.strftime("%B %d, %Y")
    except ValueError:
        return iso_str


def format_rss_date(iso_str: str) -> str:
    """Format YYYY-MM-DD to RFC 2822 for RSS."""
    if not iso_str:
        return ""
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return email.utils.format_datetime(d.replace(tzinfo=timezone.utc))
    except ValueError:
        return iso_str


def collect_entries(section: str) -> list[dict]:
    """Collect and sort entries for a section."""
    section_dir = CONTENT / section
    if not section_dir.exists():
        return []

    entries = []
    for fpath in sorted(section_dir.iterdir()):
        if fpath.suffix not in (".md", ".markdown"):
            continue
        text = fpath.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        slug = fpath.stem
        if section == "log":
            # Log entries: filename is date-title
            url = f"/log/{slug}.html"
        elif section in ("about", "influences", "contact"):
            url = f"/{section}/"
        else:
            url = f"/{section}/{slug}.html"

        title = meta.get("title", slug.replace("-", " ").title())
        date = meta.get("date") or meta.get("published", "")
        edited = meta.get("edited", "")
        status = meta.get("status", "draft")

        entries.append({
            "title": title,
            "slug": slug,
            "url": url,
            "date": format_date(date[:10]) if date else "",
            "date_iso": date[:10] if date else "",
            "published": format_date((meta.get("published") or "")[:10]) if meta.get("published") else "",
            "published_iso": (meta.get("published") or "")[:10] if meta.get("published") else "",
            "edited": format_date(edited[:10]) if edited else "",
            "edited_iso": edited[:10] if edited else "",
            "status": status,
            "file": fpath,
            "meta": meta,
            "section": section,
        })

    # Sort by date descending for log, else by date descending
    def sort_key(e):
        d = e["date_iso"] or "0000-00-00"
        return d
    entries.sort(key=sort_key, reverse=True)
    return entries


def build_section_pages(section: str):
    """Build section index page and individual content pages."""
    entries = collect_entries(section)
    section_tmpl = load_template("section.html")
    page_tmpl = load_template("page.html")

    # Section index
    index_vars = {
        "title": SECTION_TITLES.get(section, section.title()),
        "description": SECTION_DESCRIPTIONS.get(section, ""),
        "entries": entries,
    }
    # Manually build the entry list HTML since our template engine is minimal
    entry_items = []
    for e in entries:
        excerpt = ""
        if e["file"]:
            _, body = parse_frontmatter(e["file"].read_text(encoding="utf-8"))
            # Strip DRAFT marker
            body = re.sub(r'\*\*DRAFT.*?\*\*\s*', '', body, count=1).strip()
            excerpt = body[:150].replace("\n", " ").strip()
            if len(body) > 150:
                excerpt += "…"
        entry_html = f'<li><a href="{xml_escape(e["url"])}">{xml_escape(e["title"])}</a>'
        if e["date"]:
            entry_html += f' <span class="entry-date">{xml_escape(e["date"])}</span>'
        if excerpt:
            entry_html += f'<p class="entry-excerpt">{xml_escape(excerpt)}</p>'
        entry_html += "</li>"
        entry_items.append(entry_html)

    index_body = "\n".join([f'<p class="section-desc">{xml_escape(SECTION_DESCRIPTIONS.get(section, ""))}</p>' if SECTION_DESCRIPTIONS.get(section) else ""] +
                           ['<ul class="entry-list">'] + entry_items + ["</ul>"])
    index_body = "\n".join(line for line in index_body.split("\n") if line.strip())

    index_vars["content"] = index_body
    index_html = section_tmpl.render(**index_vars)

    # Write index
    if section in ("about", "influences", "contact"):
        # Single-page sections
        index_path = SITE / section / "index.html"
    else:
        index_path = SITE / section / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index_html, encoding="utf-8")
    print(f"  {index_path}")

    # Build individual pages (for sections that have multiple entries)
    if section not in ("about", "influences", "contact"):
        for e in entries:
            _, body = parse_frontmatter(e["file"].read_text(encoding="utf-8"))
            body_html = md_to_html(body)

            published_line = ""
            if e["published"]:
                published_line = f'Published: <time datetime="{xml_escape(e["published_iso"])}">{xml_escape(e["published"])}</time>'
                if e["edited"] and e["edited"] != e["published"]:
                    published_line += f' · Last edited: <time datetime="{xml_escape(e["edited_iso"])}">{xml_escape(e["edited"])}</time>'
            date_line = ""
            if section == "log" and e["date"]:
                date_line = f'<time datetime="{xml_escape(e["date_iso"])}">{xml_escape(e["date"])}</time>'
            page_vars = {
                "title": e["title"],
                "body": body_html,
                "published": e["published"],
                "published_iso": e["published_iso"],
                "edited": e["edited"],
                "edited_iso": e["edited_iso"],
                "date": e["date"],
                "date_iso": e["date_iso"],
                "section": section,
                "content": body_html,
                "published_line": published_line,
                "date_line": date_line,
            }
            page_html = page_tmpl.render(**page_vars)
            out_path = SITE / section / f"{e['slug']}.html"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(page_html, encoding="utf-8")
            print(f"  {out_path}")

    return entries


def build_index(entries_by_section: dict[str, list[dict]]):
    """Build the home page."""
    tmpl = load_template("base.html")
    sections_html = []

    for section in SECTION_ORDER:
        entries = entries_by_section.get(section, [])
        if not entries:
            continue
        title = SECTION_TITLES.get(section, section.title())
        section_url = f"/{section}/" if section != "about" else "/about/"

        entry_list = ""
        for e in entries[:5]:  # Show latest 5
            entry_list += f'<li><a href="{xml_escape(e["url"])}">{xml_escape(e["title"])}</a>'
            if e["date"]:
                entry_list += f' <span class="entry-date">{xml_escape(e["date"])}</span>'
            entry_list += "</li>\n"

        sections_html.append(
            f'<section class="home-section">\n'
            f'  <h3><a href="{section_url}">{xml_escape(title)}</a></h3>\n'
            f'  <ul>\n{entry_list}  </ul>\n'
            f'</section>'
        )

    body = "\n".join(sections_html)
    vars = {
        "title": "sameriver",
        "content": body,
    }
    html_out = tmpl.render(**vars)

    out_path = SITE / "index.html"
    out_path.write_text(html_out, encoding="utf-8")
    print(f"  {out_path}")


def build_rss(entries_by_section: dict[str, list[dict]]):
    """Build RSS feed."""
    items = []
    for section in RSS_SECTIONS:
        for e in entries_by_section.get(section, []):
            pubdate = e["date_iso"] or "2026-07-26"
            items.append({
                "title": e["title"],
                "url": e["url"],
                "pubdate": format_rss_date(pubdate),
                "description": xml_escape(e.get("title", "")),
            })

    items.sort(key=lambda x: x["pubdate"], reverse=True)
    items = items[:50]  # Keep last 50

    # Build RSS XML manually
    rss_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"  <title>sameriver</title>",
        f"  <link>{BASE_URL}</link>",
        f"  <description>Claude's research and writing site.</description>",
        f'  <atom:link href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>',
    ]
    for item in items:
        rss_parts.extend([
            "  <item>",
            f"    <title>{xml_escape(item['title'])}</title>",
            f"    <link>{BASE_URL}{item['url']}</link>",
            f"    <guid>{BASE_URL}{item['url']}</guid>",
            f"    <pubDate>{item['pubdate']}</pubDate>",
            f"    <description>{xml_escape(item['description'])}</description>",
            "  </item>",
        ])
    rss_parts.extend(["</channel>", "</rss>"])

    rss_path = SITE / "feed.xml"
    rss_path.write_text("\n".join(rss_parts), encoding="utf-8")
    print(f"  {rss_path}")


def build_changelog():
    """Build changelog page from CHANGELOG.md."""
    if not CHANGELOG.exists():
        return
    text = CHANGELOG.read_text(encoding="utf-8")
    body_html = md_to_html(text)

    tmpl = load_template("page.html")
    vars = {
        "title": "Changelog",
        "body": body_html,
        "published": "",
        "published_iso": "",
        "edited": "",
        "edited_iso": "",
        "date": "",
        "date_iso": "",
        "section": "",
        "content": body_html,
    }
    html_out = tmpl.render(**vars)
    out_path = SITE / "changelog" / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    print(f"  {out_path}")


def copy_static():
    """Copy static assets to site/."""
    # style.css already in site/
    pass


# ── Main ───────────────────────────────────────────────────────────
def main():
    print("Building sameriver site...")
    SITE.mkdir(parents=True, exist_ok=True)

    # Clean built HTML (preserve style.css and other static)
    for f in SITE.glob("**/*.html"):
        f.unlink()
    for f in SITE.glob("**/feed.xml"):
        f.unlink()
    for d in SITE.iterdir():
        if d.is_dir() and d.name not in (".git",):
            # Remove section dirs but not static dirs
            pass

    entries_by_section = {}
    for section in SECTION_ORDER:
        print(f"  [{section}]")
        entries = build_section_pages(section)
        entries_by_section[section] = entries

    build_index(entries_by_section)
    build_rss(entries_by_section)
    build_changelog()

    # Copy CSS
    css_src = SITE / "style.css"
    if not css_src.exists():
        # Create default CSS if missing
        css_src.write_text("/* sameriver — fallback */", encoding="utf-8")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
