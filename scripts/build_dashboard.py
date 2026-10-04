import html
import os
import re
import sys
import argparse
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from common import DATA_REPO_URL, SUMMARY_FAILED, SUMMARY_INITIAL, SUMMARY_UNAVAILABLE
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


def to_jst(value, with_suffix: bool = True) -> str:
    try:
        text = parse_timestamp(value).astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value or "")
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
            "updates": ups,
            "count": len(real),
            "first": to_jst(ups[-1]["created_at"], with_suffix=False)[:10] if ups else "",
            "last": to_jst(real[0]["created_at"]) if real else "",
            "excerpt": next((e for e in (excerpt(u.get("summary")) for u in real) if e), ""),
            "wayback": wayback,
        })
    return sites


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

def write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Generated {path}")


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

    os.makedirs(public_dir, exist_ok=True)
    attach_diff_pages(updates, public_dir)

    # Summaries derive from untrusted web content, so escape everything (render_markdown escapes first).
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html"]))
    sites = build_site_infos(configured_sites(args.data_dir), updates)
    names = {s["slug"]: s["name"] for s in sites}

    env.filters.update(jst=to_jst, hostname=hostname, md=render_markdown,
                       site_name=lambda slug: names.get(slug, slug))
    env.globals.update(initial_summary=SUMMARY_INITIAL, data_repo_url=DATA_REPO_URL)

    generated = to_jst(datetime.now(timezone.utc))
    stats = {
        "updates": len(updates),
        "sites": len({u["site_slug"] for u in updates if u.get("site_slug")}),
        "latest": to_jst(updates[0]["created_at"], with_suffix=False)[:10] if updates else "-",
    }

    # Stylesheets
    if os.path.isdir(static_dir):
        shutil.copytree(static_dir, os.path.join(public_dir, "assets"), dirs_exist_ok=True)

    # Three views of the same timeline (GitHub-style / dashboard / minimal)
    for page in ROOT_PAGES:
        rendered = env.get_template(page).render(updates=updates, stats=stats, generated=generated, sites=sites)
        write(os.path.join(public_dir, page), rendered)

    # Overview of every monitored site, and one history page per site (also for sites without updates yet)
    write(os.path.join(public_dir, "sites.html"),
          env.get_template("sites.html").render(sites=sites, generated=generated))
    site_template = env.get_template("site_detail.html")
    for site in sites:
        rendered = site_template.render(site=site, updates=site["updates"], generated=generated, sites=sites)
        write(os.path.join(public_dir, "sites", site["slug"], "index.html"), rendered)

    if not commit_and_push_parent(args.data_dir):
        sys.exit(1)


if __name__ == "__main__":
    main()
