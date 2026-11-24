"""The machine-readable files of the dashboard: search.json, feeds/status.json, the Atom feeds and sitemap.xml."""
import hashlib
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

from canonical import canonical_url
from common import SITE_PAGE, STALK_RUNS, STATUS_DELAY_MIN, STATUS_WINDOW_MIN, SUMMARY_INITIAL
from dashboard_text import excerpt, jst_datetime, parse_timestamp, plain_summary, render_markdown, summary_by, to_jst

def build_search_index(sites: list, updates: list, groups: list = ()) -> str:
    """JSON for the client-side full-text search: every site and every update summary, no server needed.

    {"v":1,"sites":[[slug,name,url,[tags]]...],"groups":[[heading,[tag...]]...],"updates":[[slug,"YYYY-MM-DD HH:MM",hash7,text,diff_file]...]}

    `groups` = the `tag_groups` of config.yaml (headings and their tags, in the order written); the client apps use it to let
    a user pick categories. A reader that does not know `groups` ignores it.
    """
    rows = []
    for u in updates:  # newest first
        text = plain_summary(u.get("summary"))
        if text and u.get("site_slug"):
            rows.append([u["site_slug"], to_jst(u["created_at"], with_suffix=False, weekday=True), str(u.get("commit_hash", ""))[:7],
                         text, u.get("diff_file") or ""])
    return json.dumps({"v": 1,
                       "sites": [[s["slug"], s["name"], s.get("url", ""), s.get("tags", [])] for s in sites],
                       "groups": [[g, list(names)] for g, names in groups],
                       "updates": rows}, ensure_ascii=False, separators=(",", ":"))


def stalk_runs() -> list:
    """The run times (JST "HH:MM") from STALK_RUNS (env or common.py); entries that are not HH:MM are ignored."""
    text = os.environ.get("STALK_RUNS") or STALK_RUNS
    runs = [r.strip() for r in text.split(",") if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", r.strip())]
    return sorted(set(runs)) or sorted(set(STALK_RUNS.split(",")))


def build_status(sites: list, generated: str) -> str:
    """feeds/status.json: the smallest thing a client app has to fetch to learn whether anything changed.

    {"v":1,"generated":"<UTC>","tz":"Asia/Tokyo","utcOffsetMin":540,"runs":["07:30",...],"delayMin":8,"windowMin":25,
     "sites":{slug: <7-character hash of the site's newest real update>}}
    Sites without a real update (only the first snapshot) are left out, like in their Atom feed."""
    heads = {}
    for s in sites:
        real = next((u for u in s.get("updates", []) if u.get("summary") != SUMMARY_INITIAL), None)
        if real and real.get("commit_hash"):
            heads[s["slug"]] = str(real["commit_hash"])[:7]
    return json.dumps({"v": 1, "generated": generated, "tz": "Asia/Tokyo", "utcOffsetMin": 540, "runs": stalk_runs(),
                       "delayMin": int(os.environ.get("STATUS_DELAY_MIN") or STATUS_DELAY_MIN),
                       "windowMin": int(os.environ.get("STATUS_WINDOW_MIN") or STATUS_WINDOW_MIN), "sites": heads},
                      ensure_ascii=False, separators=(",", ":"))


def previous_generated(path: str):
    """The `generated` of the status.json that is already in public/ (None when there is none)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("generated")
    except (OSError, ValueError):
        return None


def tag_id(tag: str) -> str:
    """ASCII-safe id for a (possibly Japanese) tag; used in feed file names."""
    return "tag-" + hashlib.sha1(tag.encode("utf-8")).hexdigest()[:8]


ATOM_NS = "http://www.w3.org/2005/Atom"


ET.register_namespace("", ATOM_NS)


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def build_atom(title: str, feed_path: str, page_path: str, entries: list, names: dict, base_url: str) -> str:
    """Atom 1.0 feed for the given updates (initial snapshots are not updates and are skipped)."""
    base = base_url.rstrip("/") + "/"
    host = urlparse(base).netloc or "g-i-t-data"
    entries = [u for u in entries if u.get("summary") != SUMMARY_INITIAL]
    stamps = [parse_timestamp(u["created_at"]) for u in entries]
    updated = max(stamps) if stamps else datetime.now(timezone.utc)

    feed = ET.Element(_atom("feed"))
    ET.SubElement(feed, _atom("title")).text = title
    ET.SubElement(feed, _atom("id")).text = f"tag:{host},2026:g-i-t-data/{feed_path}"
    ET.SubElement(feed, _atom("updated")).text = updated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ET.SubElement(feed, _atom("link"), rel="self", type="application/atom+xml", href=base + feed_path)
    ET.SubElement(feed, _atom("link"), rel="alternate", type="text/html", href=base + page_path)
    ET.SubElement(ET.SubElement(feed, _atom("author")), _atom("name")).text = "G醫t"

    for u, stamp in zip(entries, stamps):
        slug = u.get("site_slug", "")
        name = names.get(slug, slug)
        line = excerpt(u.get("summary"))
        entry = ET.SubElement(feed, _atom("entry"))
        ET.SubElement(entry, _atom("title")).text = f"{name}: {line}" if line else f"{name}: 更新を検知"
        ET.SubElement(entry, _atom("id")).text = f"tag:{host},2026:g-i-t-data/{slug}/{u.get('commit_hash', '')}"
        ET.SubElement(entry, _atom("updated")).text = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ET.SubElement(entry, _atom("link"), rel="alternate", type="text/html", href=f"{base}sites/{slug}/{SITE_PAGE}")
        links = [f'<a href="{html.escape(u.get("url", ""), quote=True)}">確認先のページ</a>']
        if u.get("diff_file"):
            links.append(f'<a href="{base}sites/{slug}/{u["diff_file"]}">差分</a>')
        for a in u.get("archives", []):
            if a.get("kind") == "page" and a.get("status") == "done" and a.get("archive_url"):
                links.append(f'<a href="{html.escape(a["archive_url"], quote=True)}">Wayback Machine</a>')
        by = summary_by(u.get("summary_model"))
        body = (str(render_markdown(u.get("summary"))) + (f"<p><small>{html.escape(by)}</small></p>" if by else "")
                + "<p>" + " ・ ".join(links) + "</p>")
        ET.SubElement(entry, _atom("content"), type="html").text = body

    ET.indent(feed)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


def _lastmod(updates: list):
    """W3C date (JST) of the newest update in the list, or None."""
    best = None
    for u in updates:
        try:
            dt = jst_datetime(u["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or dt > best:
            best = dt
    return best.strftime("%Y-%m-%d") if best else None


def build_sitemap(base: str, sites: list, months: list, weeks: list, updates: list) -> str:
    """sitemap.xml: the pages worth finding in a search engine (timeline, site list, About, archive, one history page per site).
    Not listed: the diff pages (thousands of near-empty comparison pages), dashboard.html / minimal.html (the same timeline in
    other styles), privacy.html and offline.html. Addresses are the canonical ones (see canonical_url)."""
    entries = [("index.html", _lastmod(updates)), ("sites.html", _lastmod(updates)), ("about.html", None), ("guide.html", None),
               ("archive/index.html", _lastmod(updates))]
    entries += [(f"archive/{g['key']}.html", _lastmod(g["updates"])) for g in weeks + months]
    entries += [(f"sites/{s['slug']}/{SITE_PAGE}", _lastmod(s["updates"])) for s in sites]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for rel, mod in entries:
        loc = html.escape(canonical_url(base, rel), quote=False)
        lines.append(f"  <url><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
