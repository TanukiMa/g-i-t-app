import hashlib
import html
import json
import os
import re
import sys
import argparse
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from common import (DATA_REPO_URL, FEED_LIMIT_ALL, FEED_LIMIT_SITE, SITE_BASE_URL, SITE_PAGE, SUMMARY_FAILED,
                    SUMMARY_INITIAL, SUMMARY_UNAVAILABLE, TIMELINE_LIMIT, LEGACY_SUMMARY_INITIAL)
from provision import configured_sites

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

PAGE_SIZE = 1000
JST = timezone(timedelta(hours=9))  # no DST, so a fixed offset is exact and needs no tzdata
ROOT_PAGES = ("index.html", "dashboard.html", "minimal.html")


# ---------------------------------------------------------------- template filters

def parse_timestamp(value) -> datetime:
    """Parse a Supabase timestamptz string (fraction may have 1-6 digits)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], text, count=1)
        dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")  # fixed: no locale needed


def to_jst(value, with_suffix: bool = True, weekday=None) -> str:
    """JST time text. Full display: "2026-10-07 Wednesday 09:46 JST".

    with_suffix=False gives the short "2026-10-07 09:46" (callers slice it: [:10] date, [11:] time);
    weekday=True puts the English weekday name after the date in that form too (default: only with the suffix).
    """
    if weekday is None:
        weekday = with_suffix
    try:
        dt = parse_timestamp(value).astimezone(JST)
    except (ValueError, TypeError):
        return str(value or "")
    day = f"{dt:%Y-%m-%d} {WEEKDAYS[dt.weekday()]}" if weekday else f"{dt:%Y-%m-%d}"
    text = f"{day} {dt:%H:%M}"
    return f"{text} JST" if with_suffix else text


def hostname(url) -> str:
    host = urlparse(str(url)).netloc
    return host[4:] if host.startswith("www.") else (host or str(url))


_BULLET = re.compile(r"^(\s*)([*+\-]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def _inline(text: str) -> str:
    text = html.escape(text, quote=True)  # escape first: nothing from the source can become markup
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def render_markdown(text) -> Markup:
    """Tiny, safe Markdown subset for AI summaries: bullets (nested), **bold**, `code`, paragraphs."""
    out, indents, para = [], [], []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_lists():
        while indents:
            out.append("</li></ul>")
            indents.pop()

    for raw in str(text or "").splitlines():
        if not raw.strip():
            flush_para()
            close_lists()
            continue
        bullet = _BULLET.match(raw.expandtabs(4))
        if bullet:
            flush_para()
            depth, content = len(bullet.group(1)), bullet.group(3)
            if not indents or depth > indents[-1]:
                out.append("<ul>")
                indents.append(depth)
            else:
                while len(indents) > 1 and depth < indents[-1]:
                    out.append("</li></ul>")
                    indents.pop()
                out.append("</li>")
            out.append("<li>" + _inline(content))
        elif indents:
            out.append(" " + _inline(raw.strip()))  # continuation of the current list item
        else:
            heading = _HEADING.match(raw.strip())
            if heading:
                flush_para()
                out.append("<p><strong>" + _inline(heading.group(1)) + "</strong></p>")
            else:
                para.append(raw.strip())
    flush_para()
    close_lists()
    return Markup("".join(out))


def excerpt(summary, limit: int = 90) -> str:
    """One plain-text line of an AI summary for the site list ("" when there is nothing useful)."""
    if not summary or summary in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
        return ""
    for raw in str(summary).splitlines():
        line = re.sub(r"^\s*(?:[*+\-]|\d+[.)]|#{1,6})\s+", "", raw).replace("**", "").replace("`", "").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def plain_summary(summary) -> str:
    """Whole AI summary as one plain-text line for the search index ("" when there is no real summary)."""
    if not summary or summary in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
        return ""
    lines = (re.sub(r"^\s*(?:[*+\-]|\d+[.)]|#{1,6})\s+", "", raw).replace("**", "").replace("`", "").strip()
             for raw in str(summary).splitlines())
    return " ".join(line for line in lines if line)


def build_search_index(sites: list, updates: list) -> str:
    """JSON for the client-side full-text search: every site and every update summary, no server needed.

    {"v":1,"sites":[[slug,name,url,[tags]]...],"updates":[[slug,"YYYY-MM-DD HH:MM",hash7,text,diff_file]...]}
    """
    rows = []
    for u in updates:  # newest first
        text = plain_summary(u.get("summary"))
        if text and u.get("site_slug"):
            rows.append([u["site_slug"], to_jst(u["created_at"], with_suffix=False, weekday=True), str(u.get("commit_hash", ""))[:7],
                         text, u.get("diff_file") or ""])
    return json.dumps({"v": 1,
                       "sites": [[s["slug"], s["name"], s.get("url", ""), s.get("tags", [])] for s in sites],
                       "updates": rows}, ensure_ascii=False, separators=(",", ":"))


def build_site_infos(configured: list, updates: list) -> list:
    """One record per monitored site (config order, then sites only known from old updates)."""
    by_slug = {}
    for u in updates:  # updates are newest first
        by_slug.setdefault(u.get("site_slug"), []).append(u)

    sites, seen = [], set()
    for site in configured + [{"slug": s, "name": s, "url": (ups[0].get("url") or "")}
                              for s, ups in by_slug.items() if s]:
        slug = site["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        ups = by_slug.get(slug, [])
        real = [u for u in ups if u.get("summary") != SUMMARY_INITIAL]  # the first snapshot is not an "update"
        wayback = next((a["archive_url"] for u in ups for a in u.get("archives", [])
                        if a.get("kind") == "page" and a.get("status") == "done" and a.get("archive_url")), "")
        sites.append({
            **site,
            "tags": site.get("tags", []),
            "feed": f"feeds/{slug}.xml",
            "updates": ups,
            "count": len(real),
            "first": to_jst(ups[-1]["created_at"], with_suffix=False)[:10] if ups else "",
            "last": to_jst(real[0]["created_at"]) if real else "",
            "excerpt": next((e for e in (excerpt(u.get("summary")) for u in real) if e), ""),
            "wayback": wayback,
        })
    return sites


def tag_id(tag: str) -> str:
    """ASCII-safe id for a (possibly Japanese) tag; used in feed file names."""
    return "tag-" + hashlib.sha1(tag.encode("utf-8")).hexdigest()[:8]


def build_tag_infos(sites: list) -> list:
    """[{name, id, count, feed}] sorted by name; count = number of sites carrying the tag."""
    counts = {}
    for site in sites:
        for tag in site.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"name": t, "id": tag_id(t), "count": n, "feed": f"feeds/{tag_id(t)}.xml"}
            for t, n in sorted(counts.items())]


def jst_datetime(value) -> datetime:
    return parse_timestamp(value).astimezone(JST)


def group_periods(updates: list):
    """Group updates (newest first) by JST month and ISO week.

    Returns (months, weeks): lists of {key, label, updates, count}, newest first.
    """
    months, weeks = {}, {}
    for u in updates:
        try:
            dt = jst_datetime(u["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        mkey = f"{dt.year}-{dt.month:02d}"
        iso = dt.isocalendar()
        wkey = f"{iso[0]}-W{iso[1]:02d}"
        months.setdefault(mkey, {"key": mkey, "label": f"{dt.year}年{dt.month}月", "updates": []})["updates"].append(u)
        if wkey not in weeks:
            monday = date.fromisocalendar(iso[0], iso[1], 1)
            sunday = monday + timedelta(days=6)
            weeks[wkey] = {"key": wkey, "label": f"{iso[0]}年第{iso[1]}週",
                           "range": f"{monday.month}/{monday.day}〜{sunday.month}/{sunday.day}", "updates": []}
        weeks[wkey]["updates"].append(u)

    def finish(groups):
        out = sorted(groups.values(), key=lambda g: g["key"], reverse=True)
        for g in out:
            g["count"] = len(g["updates"])
        return out

    return finish(months), finish(weeks)


def group_by_day(updates: list) -> list:
    """[(YYYY-MM-DD (曜), [updates])] newest first, JST days."""
    youbi = "月火水木金土日"
    days = {}
    for u in updates:
        dt = jst_datetime(u["created_at"])
        days.setdefault(dt.date(), []).append(u)
    return [(f"{d.isoformat()}（{youbi[d.weekday()]}）", ups) for d, ups in sorted(days.items(), reverse=True)]


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
    ET.SubElement(ET.SubElement(feed, _atom("author")), _atom("name")).text = "G医t"

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
        body = str(render_markdown(u.get("summary"))) + "<p>" + " ・ ".join(links) + "</p>"
        ET.SubElement(entry, _atom("content"), type="html").text = body

    ET.indent(feed)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


# ---------------------------------------------------------------- data

def attach_diff_pages(updates, public_dir: str):
    """Set item['diff_file'] for updates whose diff_<hash7>.html was written by website_stalk.py."""
    for item in updates:
        slug, commit = item.get("site_slug"), item.get("commit_hash") or ""
        if not slug or not commit:
            continue
        name = f"diff_{commit[:7]}.html"
        if os.path.isfile(os.path.join(public_dir, "sites", slug, name)):
            item["diff_file"] = name


DIFF_MARK = "<!--git-diff-v2-->"
DIFF_STYLE = (
    "<style>"
    ".git-nav{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;align-items:center;margin:0;padding:.6rem 1rem;"
    "background:#f6f8fa;border-bottom:1px solid #d0d7de;font:14px/1.4 system-ui,sans-serif;text-align:left}"
    ".git-head{max-width:70rem;margin:0 auto;padding:1rem 1rem .5rem;text-align:left;font:15px/1.7 system-ui,'Hiragino Sans','Yu Gothic',sans-serif;color:#1f2328}"
    ".git-head h1{font-size:1.35rem;margin:.2rem 0 .6rem;color:inherit}"
    ".git-head dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:0 0 .8rem}"
    ".git-head dt{color:#656d76}.git-head dd{margin:0;overflow-wrap:anywhere}"
    ".git-head details{margin:.5rem 0;padding:.5rem .9rem;border:1px solid #d0d7de;border-radius:8px}"
    ".git-head summary{cursor:pointer;font-weight:600}"
    ".git-head .how{color:#656d76;font-size:.9em;margin:.4rem 0 0}"
    ".git-foot{max-width:70rem;margin:2rem auto 1.5rem;padding:0 1rem;text-align:center;font:13px/1.6 system-ui,sans-serif;color:#656d76}"
    ".git-nav a,.git-head a,.git-foot a{color:#0969da;text-decoration:none}"
    ".git-nav a:hover,.git-head a:hover,.git-foot a:hover{text-decoration:underline}"
    ".git-nav span{color:#656d76}"
    "@media (prefers-color-scheme:dark){.git-nav{background:#161b22;border-color:#30363d}"
    ".git-head{color:#e6edf3}.git-head details{border-color:#30363d}"
    ".git-head dt,.git-head .how,.git-nav span,.git-foot{color:#8b949e}"
    ".git-nav a,.git-head a,.git-foot a{color:#58a6ff}}"
    "</style>"
)
_DIFF_OLD_NAV = re.compile(r"<!--git-nav-->.*?</nav>", re.S)


def decorate_diff_pages(public_dir: str, names: dict, updates: list) -> int:
    """Make every diff page understandable on its own.

    diff2html writes a standalone page headed "Diff to HTML by rtfpessoa". Here that heading is replaced by
    what the visitor needs: which site and update this is, when, the monitored page, the commit and the AI
    summary, plus how to read the two columns. A link bar leads back, and the credit moves to the footer.
    Done for all existing diff pages (also those of earlier runs); the marker keeps it idempotent.
    Returns the number of pages changed.
    """
    by_key = {(u.get("site_slug"), str(u.get("commit_hash", ""))[:7]): u for u in updates}
    sites_root = os.path.join(public_dir, "sites")
    changed = 0
    for slug in sorted(os.listdir(sites_root)) if os.path.isdir(sites_root) else []:
        folder = os.path.join(sites_root, slug)
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("diff_") and name.endswith(".html")):
                continue
            path = os.path.join(folder, name)
            with open(path, "r", encoding="utf-8") as f:
                page = f.read()
            if DIFF_MARK in page:
                # decorated by an earlier build: only the link to the timeline changed (directory URL, no index.html)
                fixed = page.replace('href="../../index.html"', 'href="../../"')
                if fixed != page:
                    with open(path, "w", encoding="utf-8", newline="") as f:
                        f.write(fixed)
                    changed += 1
                continue
            if not re.search(r"<body[^>]*>", page):
                continue
            page = _DIFF_OLD_NAV.sub("", page)      # bar of the first version of this feature
            hash7 = name[5:-5]
            site = html.escape(names.get(slug, slug))
            upd = by_key.get((slug, hash7))
            when = html.escape(to_jst(upd["created_at"])) if upd else ""

            rows = []
            if when:
                rows.append(f"<dt>更新日時</dt><dd>{when}</dd>")
            if upd and upd.get("url"):
                url = html.escape(upd["url"], quote=True)
                rows.append(f'<dt>確認先のページ</dt><dd><a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></dd>')
            if upd and upd.get("commit_hash"):
                commit = html.escape(str(upd["commit_hash"]), quote=True)
                rows.append(f'<dt>記録</dt><dd><a href="{DATA_REPO_URL}/commit/{commit}" target="_blank" '
                            f'rel="noopener noreferrer">コミット {commit[:7]}</a></dd>')
            summary = ""
            text = (upd or {}).get("summary") or ""
            if text and text not in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
                summary = (f'<details open><summary>AI による要約</summary>{render_markdown(text)}'
                           '<p class="how">AI の要約は誤りを含むことがあります。下の差分、または確認先のページで確かめてください。</p></details>')
            head = (f'{DIFF_MARK}<nav class="git-nav">'
                    f'<a href="{SITE_PAGE}" onclick="if(document.referrer&&history.length>1){{history.back();return false}}">← 戻る</a>'
                    f'<a href="{SITE_PAGE}">📜 {site} の更新歴</a>'
                    f'<a href="../../">全体タイムライン</a>'
                    f'<a href="../../sites.html">サイト一覧</a></nav>'
                    f'<header class="git-head"><h1>{site} の更新差分</h1>'
                    f'<dl>{"".join(rows)}</dl>{summary}'
                    f'<p class="how">ページのテキストの、前回の確認との違いです。左が変更前、右が変更後で、'
                    f'<strong>緑</strong>が追加、<strong>赤</strong>が削除された部分です。</p></header>')
            foot = ('<footer class="git-foot"><p>G医t が記録した差分です。'
                    f'<a href="../../about.html">G医tについて</a> ・ <a href="../../">全体タイムライン</a></p>'
                    '<p>差分の表示: Diff to HTML by <a href="https://github.com/rtfpessoa">rtfpessoa</a>（diff2html）</p></footer>')

            page = re.sub(r"<h1>Diff to HTML by .*?</h1>", "", page, count=1, flags=re.S)
            page = re.sub(r"<body[^>]*>", lambda m: m.group(0) + head, page, count=1)
            page = page.replace("</body>", foot + "</body>", 1)
            page = page.replace("</head>", DIFF_STYLE + "</head>", 1)
            page = re.sub(r"<title>.*?</title>", lambda m: f"<title>差分 {site} {when or hash7} - G医t</title>", page, count=1, flags=re.S)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(page)
            changed += 1
    return changed


def normalize_legacy_summaries(updates: list) -> int:
    """Show the first-snapshot rows written with an older text as the current SUMMARY_INITIAL (Supabase is not touched)."""
    n = 0
    for u in updates:
        if u.get("summary") in LEGACY_SUMMARY_INITIAL:
            u["summary"] = SUMMARY_INITIAL
            n += 1
    return n


def fetch_all(supabase, table: str) -> list:
    """Page through a table; PostgREST caps each response (1000 by default)."""
    rows = []
    while True:
        res = (supabase.table(table).select("*")
               .order("id", desc=True)
               .range(len(rows), len(rows) + PAGE_SIZE - 1).execute())
        rows.extend(res.data or [])
        if len(res.data or []) < PAGE_SIZE:
            return rows


def fetch_updates_from_supabase():
    """Return updates (newest first), each with an 'archives' list from archive_queue."""
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_KEY")

    if not create_client or not supa_url or not supa_key:
        print("Supabase config missing.")
        return None

    try:
        supabase: Client = create_client(supa_url, supa_key)
        updates = fetch_all(supabase, "updates")
        queue = fetch_all(supabase, "archive_queue")
    except Exception as e:
        print(f"Error fetching from Supabase: {e}")
        return None

    archives = {}
    for q in sorted(queue, key=lambda r: (r["kind"] != "page", r["id"])):
        archives.setdefault((q["site_slug"], q["commit_hash"]), []).append(q)
    for u in updates:
        u["archives"] = archives.get((u["site_slug"], u["commit_hash"]), [])
    updates.sort(key=lambda u: u.get("created_at") or "", reverse=True)
    return updates


# ---------------------------------------------------------------- git

def git(args, cwd):
    print(f"Executing in {cwd}: git {' '.join(args)}")
    res = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 and "--quiet" not in args:  # --quiet uses exit 1 as "has changes"
        print(f"git {args[0]} failed ({res.returncode}): {res.stderr}")
    return res


def commit_and_push_parent(data_dir: str) -> bool:
    """Commit the regenerated public/ (and anything else pending) and push g-i-t-data."""
    git(["add", "-A"], data_dir)
    if git(["diff", "--cached", "--quiet"], data_dir).returncode == 0:
        print("No parent repository changes to commit.")
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if git(["commit", "-m", f"Snapshot {stamp}"], data_dir).returncode != 0:
        return False
    return git(["push", "origin", "HEAD"], data_dir).returncode == 0


# ---------------------------------------------------------------- build

_GA_ID_RE = re.compile(r"^G-[A-Z0-9]{4,20}$")
_CF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def analytics_settings() -> dict:
    """Analytics IDs from the environment. Empty / invalid values switch that service off."""
    ga_id = (os.environ.get("GA_MEASUREMENT_ID") or "").strip()
    cf_token = (os.environ.get("CF_BEACON_TOKEN") or "").strip()
    if ga_id and not _GA_ID_RE.match(ga_id):
        print(f"WARNING: GA_MEASUREMENT_ID {ga_id!r} does not look like G-XXXXXXXXXX; Google Analytics is off.")
        ga_id = ""
    if cf_token and not _CF_TOKEN_RE.match(cf_token):
        print("WARNING: CF_BEACON_TOKEN has an unexpected format; Cloudflare Web Analytics is off.")
        cf_token = ""
    return {"ga_id": ga_id, "cf_token": cf_token, "enabled": bool(ga_id or cf_token)}

def write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Generated {path}")


def asset_version(static_dir: str) -> str:
    """Short hash of the CSS/JS/icons (not of the build time): the URL of an asset changes only when the asset does."""
    digest = hashlib.sha1()
    for folder, dirs, files in os.walk(static_dir):
        dirs[:] = sorted(d for d in dirs if d != "root")
        for name in sorted(files):
            path = os.path.join(folder, name)
            digest.update(os.path.relpath(path, static_dir).replace(os.sep, "/").encode("utf-8"))
            with open(path, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()[:10]


def publish_root_files(src_dir: str, public_dir: str, build_id: str, version: str = ""):
    """Copy manifest.webmanifest / sw.js to the site root (a service worker only controls its own directory)."""
    if not os.path.isdir(src_dir):
        return
    for name in os.listdir(src_dir):
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().replace("__BUILD_ID__", build_id).replace("__ASSET_VERSION__", version)
        write(os.path.join(public_dir, name), text)


def main():
    parser = argparse.ArgumentParser(description="Build static dashboard for G-I-T")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    templates_dir = os.path.join(app_dir, "templates")
    static_dir = os.path.join(app_dir, "static")
    public_dir = os.path.join(args.data_dir, "public")

    # Fetch updates first: on failure keep the previously published dashboard
    updates = fetch_updates_from_supabase()
    if updates is None:
        print("Could not fetch updates; leaving public/ untouched.")
        commit_and_push_parent(args.data_dir)  # still publish new diff pages
        sys.exit(1)
    print(f"Rewrote {normalize_legacy_summaries(updates)} first-snapshot summar(ies) from the old text.")

    os.makedirs(public_dir, exist_ok=True)
    attach_diff_pages(updates, public_dir)

    # Summaries derive from untrusted web content, so escape everything (render_markdown escapes first).
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html"]))
    sites = build_site_infos(configured_sites(args.data_dir), updates)
    names = {s["slug"]: s["name"] for s in sites}

    site_by_slug = {s_["slug"]: s_ for s_ in sites}
    env.filters.update(
        jst=to_jst, hostname=hostname, md=render_markdown, excerpt=excerpt,
        jst_hm=lambda v: to_jst(v, with_suffix=False)[11:],
        site_name=lambda slug: names.get(slug, slug),
        site_tags=lambda slug: "|".join(site_by_slug.get(slug, {}).get("tags", [])),
    )
    env.globals.update(initial_summary=SUMMARY_INITIAL, data_repo_url=DATA_REPO_URL, site_page=SITE_PAGE,
                       analytics=analytics_settings())

    print(f"Decorated {decorate_diff_pages(public_dir, names, updates)} diff page(s).")

    env.globals["asset_version"] = asset_version(static_dir) if os.path.isdir(static_dir) else ""
    generated = to_jst(datetime.now(timezone.utc))
    build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # makes every deployment a new service worker
    tags = build_tag_infos(sites)
    base_url = os.environ.get("SITE_BASE_URL", SITE_BASE_URL)
    stats = {
        "updates": len(updates),
        "sites": len({u["site_slug"] for u in updates if u.get("site_slug")}),
        "latest": to_jst(updates[0]["created_at"]) if updates else "-",
    }
    common = dict(generated=generated, sites=sites, tags=tags, total_updates=len(updates))

    # Stylesheets, scripts, icons ... -> assets/. static/root/ holds the files that must live at the site root.
    if os.path.isdir(static_dir):
        shutil.copytree(static_dir, os.path.join(public_dir, "assets"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("root"))
        publish_root_files(os.path.join(static_dir, "root"), public_dir, build_id, env.globals["asset_version"])
    write(os.path.join(public_dir, "offline.html"), env.get_template("offline.html").render())

    # Three views of the latest part of the timeline (GitHub-style / dashboard / minimal)
    timeline = updates[:TIMELINE_LIMIT]
    for page in ROOT_PAGES:
        rendered = env.get_template(page).render(
            updates=timeline, stats=stats, limit=TIMELINE_LIMIT, truncated=len(updates) > TIMELINE_LIMIT, **common)
        write(os.path.join(public_dir, page), rendered)

    # Overview of every monitored site, and one history page per site (also for sites without updates yet)
    write(os.path.join(public_dir, "sites.html"), env.get_template("sites.html").render(**common))
    site_template = env.get_template("site_detail.html")
    for site in sites:
        rendered = site_template.render(site=site, updates=site["updates"], **common)
        write(os.path.join(public_dir, "sites", site["slug"], SITE_PAGE), rendered)

    # Migration: site pages used to be sites/<slug>/index.html; drop the stale copies from public/.
    sites_root = os.path.join(public_dir, "sites")
    for entry in os.listdir(sites_root) if os.path.isdir(sites_root) else []:
        legacy = os.path.join(sites_root, entry, "index.html")
        if os.path.isfile(legacy):
            os.remove(legacy)
            print(f"Removed legacy {legacy}")

    # About page (design philosophy + the one-picture explanation)
    write(os.path.join(public_dir, "about.html"), env.get_template("about.html").render(**common))

    # Privacy and analytics notice (the text follows which analytics IDs are configured)
    write(os.path.join(public_dir, "privacy.html"), env.get_template("privacy.html").render(**common))

    # Archive: everything older than the timeline, by week and by month
    months, weeks = group_periods(updates)
    write(os.path.join(public_dir, "archive", "index.html"),
          env.get_template("archive_index.html").render(months=months, weeks=weeks, **common))
    for i, week in enumerate(weeks):
        write(os.path.join(public_dir, "archive", f"{week['key']}.html"),
              env.get_template("archive_week.html").render(
                  period=week, newer=weeks[i - 1] if i > 0 else None,
                  older=weeks[i + 1] if i + 1 < len(weeks) else None, **common))
    for i, month in enumerate(months):
        write(os.path.join(public_dir, "archive", f"{month['key']}.html"),
              env.get_template("archive_month.html").render(
                  period=month, days=group_by_day(month["updates"]), newer=months[i - 1] if i > 0 else None,
                  older=months[i + 1] if i + 1 < len(months) else None, **common))

    # Full-text search index (loaded by assets/app.js when the visitor starts searching)
    write(os.path.join(public_dir, "search.json"), build_search_index(sites, updates))

    # Atom feeds: all sites, per site, per tag
    write(os.path.join(public_dir, "feeds", "all.xml"),
          build_atom("G医t 更新情報（すべて）", "feeds/all.xml", "", updates[:FEED_LIMIT_ALL], names, base_url))
    for site in sites:
        write(os.path.join(public_dir, "feeds", f"{site['slug']}.xml"),
              build_atom(f"G医t {site['name']}", site["feed"], f"sites/{site['slug']}/{SITE_PAGE}",
                         site["updates"][:FEED_LIMIT_SITE], names, base_url))
    for tag in tags:
        tagged = [u for u in updates if tag["name"] in site_by_slug.get(u.get("site_slug"), {}).get("tags", [])]
        write(os.path.join(public_dir, "feeds", f"{tag['id']}.xml"),
              build_atom(f"G医t 分類: {tag['name']}", tag["feed"], "sites.html", tagged[:FEED_LIMIT_SITE], names, base_url))

    if not commit_and_push_parent(args.data_dir):
        sys.exit(1)


if __name__ == "__main__":
    main()
