"""Build the static dashboard (g-i-t-data/public/) from the rows in Supabase, then commit and push g-i-t-data.

The parts: dashboard_text (times, Markdown, grouping), feeds (search.json, status.json, Atom, sitemap), canonical (one
address per page), diff_pages (the diff pages). This file reads the data, renders the pages in order and publishes them.
"""
import argparse
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from canonical import canonical_base, inject_canonical
from common import (DATA_REPO_URL, FEED_LIMIT_ALL, FEED_LIMIT_SITE, LEGACY_SUMMARY_INITIAL, SITE_BASE_URL, SITE_PAGE,
                    SUMMARY_INITIAL, TIMELINE_LIMIT, env_flag)
from dashboard_text import excerpt, group_by_day, group_periods, hostname, iso_attr, render_markdown, summary_by, to_jst
from db import fetch_all, get_supabase
from diff_pages import (attach_diff_pages, canonicalize_diff_pages, count_unshared, decorate_diff_pages,
                        externalize_diff_assets, make_diff_pages, select_diff_pages)
from feeds import build_atom, build_search_index, build_sitemap, build_status, previous_generated, tag_id
from provision import config_problems, configured_sites, configured_tag_groups
from shell import git

ROOT_PAGES = ("index.html", "dashboard.html", "minimal.html")


# ---------------------------------------------------------------- data

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


def build_tag_infos(sites: list, groups: list = ()) -> list:
    """[{name, id, count, feed, group}]; count = number of sites carrying the tag. With `tag_groups` in config.yaml the tags
    come in the order of the groups (`group` = the heading); tags in no group follow, sorted by name, with group ""."""
    counts = {}
    for site in sites:
        for tag in site.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    ordered, seen = [], set()
    for group, names in groups:
        for t in names:
            if t in counts and t not in seen:
                ordered.append((t, group))
                seen.add(t)
    ordered += [(t, "") for t in sorted(counts) if t not in seen]
    return [{"name": t, "id": tag_id(t), "count": counts[t], "feed": f"feeds/{tag_id(t)}.xml", "group": g}
            for t, g in ordered]


def normalize_legacy_summaries(updates: list) -> int:
    """Show the first-snapshot rows written with an older text as the current SUMMARY_INITIAL (Supabase is not touched)."""
    n = 0
    for u in updates:
        if u.get("summary") in LEGACY_SUMMARY_INITIAL:
            u["summary"] = SUMMARY_INITIAL
            n += 1
    return n


def fetch_updates_from_supabase():
    """Return updates (newest first), each with an 'archives' list from archive_queue; None when they cannot be read."""
    supabase = get_supabase()
    if not supabase:
        return None
    try:
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

def commit_and_push_parent(data_dir: str) -> bool:
    """Commit the regenerated public/ (and anything else pending) and push g-i-t-data."""
    git(["add", "-A"], data_dir)
    if git(["diff", "--cached", "--quiet"], data_dir, ok_codes=(0, 1)).returncode == 0:   # exit 1 = "has changes"
        print("No parent repository changes to commit.")
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if git(["commit", "-m", f"Snapshot {stamp}"], data_dir).returncode != 0:
        return False
    return git(["push", "origin", "HEAD"], data_dir).returncode == 0


# ---------------------------------------------------------------- settings from the environment

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


def pwa_passphrase_hash() -> str:
    """PWA_PASSPHRASE_HASH ("pbkdf2$sha256$<iterations>$<salt>$<hash>", from scripts/pwa-passphrase.py); "" when missing or malformed."""
    value = os.environ.get("PWA_PASSPHRASE_HASH", "").strip()
    return value if re.fullmatch(r"pbkdf2\$sha256\$\d{4,7}\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+", value) else ""


def pwa_mode() -> str:
    """PWA_ENABLED: "on" (unset, 1), "off" (0 / false / no / off) or "optin" (the app is off for visitors; whoever opens
    <site>/?pwa and types the passphrase switches it on in their own browser). optin needs PWA_PASSPHRASE_HASH; without it the app is off."""
    value = os.environ.get("PWA_ENABLED", "1").strip().lower()
    if value in ("0", "false", "no", "off"):
        return "off"
    if value == "optin":
        if pwa_passphrase_hash():
            return "optin"
        print("WARNING: PWA_ENABLED=optin needs a valid PWA_PASSPHRASE_HASH (scripts/pwa-passphrase.py): the app stays off.")
        return "off"
    return "on"


def done_file():
    """DASHBOARD_DONE_FILE: a file outside the repository that proves a build ran to the end (container/entrypoint.sh
    deploys only when it exists, so a build that died, e.g. out of memory, never publishes the old pages as if new)."""
    return os.environ.get("DASHBOARD_DONE_FILE", "").strip()


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


# ---------------------------------------------------------------- writing

class PageWriter:
    """Writes the files of public/. Every HTML page (not offline.html) gets the canonical link of its address."""

    def __init__(self, public_dir: str, base):
        self.public_dir, self.base = public_dir, base

    def write(self, path: str, content: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rel = os.path.relpath(path, self.public_dir)
        if rel.endswith(".html") and not rel.startswith("..") and rel.replace(os.sep, "/") != "offline.html":
            content = inject_canonical(content, rel, self.base)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Generated {path}")

    def path(self, *parts) -> str:
        return os.path.join(self.public_dir, *parts)

    def page(self, rel: str, content: str):
        self.write(self.path(*rel.split("/")), content)

    def remove(self, rel: str, why: str = ""):
        path = self.path(*rel.split("/"))
        if os.path.isfile(path):
            os.remove(path)
            print(f"Removed {path}" + (f" ({why})" if why else ""))


def publish_root_files(writer: PageWriter, src_dir: str, build_id: str, version: str = "", mode: str = "on"):
    """Copy manifest.webmanifest / sw.js to the site root (a service worker only controls its own directory).

    mode "on":    manifest.webmanifest and sw.js (the full worker).
    mode "off":   no manifest (removed when an earlier build left one); sw.js is the retiring worker (static/sw-retire.js):
                  browsers that installed the app earlier fetch it and clean themselves up.
    mode "optin": the manifest is published (the pages do not link it; static/pwa.js adds the link in a browser that was
                  switched on), sw.js is the retiring worker as in "off", and the full worker is sw-app.js, which only
                  such a browser registers.
    """
    if not os.path.isdir(src_dir):
        return

    def render(path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().replace("__BUILD_ID__", build_id).replace("__ASSET_VERSION__", version)

    for name in os.listdir(src_dir):
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path):
            continue
        if mode == "off" and name == "manifest.webmanifest":
            writer.remove(name, "PWA_ENABLED=0")
            continue
        if mode != "on" and name == "sw.js":
            writer.page("sw.js", render(os.path.join(os.path.dirname(src_dir), "sw-retire.js")))
            if mode == "optin":
                writer.page("sw-app.js", render(path))
            continue
        writer.page(name, render(path))
    if mode != "optin":
        writer.remove("sw-app.js", "PWA_ENABLED is not optin")


# ---------------------------------------------------------------- the pages

def make_env(templates_dir: str, sites: list, version: str) -> Environment:
    """Jinja environment with the filters and globals of every template. Summaries derive from untrusted web content, so
    everything is escaped (render_markdown escapes first)."""
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html"]))
    names = {s["slug"]: s["name"] for s in sites}
    site_by_slug = {s["slug"]: s for s in sites}
    env.filters.update(
        jst=to_jst, iso_attr=iso_attr, hostname=hostname, md=render_markdown, excerpt=excerpt, summary_by=summary_by,
        jst_hm=lambda v: to_jst(v, with_suffix=False)[11:],
        site_name=lambda slug: names.get(slug, slug),
        site_tags=lambda slug: "|".join(site_by_slug.get(slug, {}).get("tags", [])),
    )
    mode = pwa_mode()
    env.globals.update(pwa_enabled=(mode == "on"), pwa_mode=mode, pwa_hash=pwa_passphrase_hash() if mode == "optin" else "",
                       initial_summary=SUMMARY_INITIAL, data_repo_url=DATA_REPO_URL, site_page=SITE_PAGE,
                       analytics=analytics_settings(), asset_version=version)
    return env


def prepare_diff_pages(public_dir: str, configured: list, updates: list, base, migrate: bool, version: str):
    """Write the diff pages of new patches, then look after the pages of the old kind (see diff_pages.py)."""
    made = make_diff_pages(public_dir, {s_["slug"]: s_["name"] for s_ in configured}, updates, base, migrate, version)
    print(f"Diff pages written from their patches: {made}.")
    attach_diff_pages(updates, public_dir)
    return made


def update_old_diff_pages(public_dir: str, names: dict, updates: list, base, migrate: bool):
    fresh, frozen = select_diff_pages(public_dir, migrate)
    print(f"Diff pages: {len(fresh)} to make{' (all of them: --migrate-diff-pages)' if migrate else ''}, "
          f"{len(frozen)} finished ones left as they are.")
    print(f"Decorated {decorate_diff_pages(public_dir, names, updates, only=fresh)} diff page(s).")
    print(f"Moved the shared style and script out of {externalize_diff_assets(public_dir, only=fresh)} diff page(s).")
    print(f"Canonical address: {base or '(none: SITE_BASE_URL is GitHub Pages or local)'}; "
          f"{canonicalize_diff_pages(public_dir, base, only=fresh)} diff page(s) changed.")
    unshared = count_unshared(frozen)
    if unshared:
        print(f"{unshared} finished diff page(s) still hold their own copy of the style and script (about 300 KB each, in memory "
              f"in the container): run once with MIGRATE_DIFF_PAGES=1 to share them.")


def render_timelines(writer: PageWriter, env: Environment, updates: list, common: dict):
    """Three views of the latest part of the timeline (GitHub-style / dashboard / minimal)."""
    stats = {
        "updates": len(updates),
        "sites": len({u["site_slug"] for u in updates if u.get("site_slug")}),
        "latest": to_jst(updates[0]["created_at"]) if updates else "-",
    }
    timeline = updates[:TIMELINE_LIMIT]
    for page in ROOT_PAGES:
        writer.page(page, env.get_template(page).render(
            updates=timeline, stats=stats, limit=TIMELINE_LIMIT, truncated=len(updates) > TIMELINE_LIMIT, **common))


def render_site_pages(writer: PageWriter, env: Environment, sites: list, common: dict):
    """Overview of every monitored site, and one history page per site (also for sites without updates yet)."""
    writer.page("sites.html", env.get_template("sites.html").render(**common))
    site_template = env.get_template("site_detail.html")
    for site in sites:
        writer.page(f"sites/{site['slug']}/{SITE_PAGE}", site_template.render(site=site, updates=site["updates"], **common))
    # Migration: site pages used to be sites/<slug>/index.html; drop the stale copies from public/.
    sites_root = writer.path("sites")
    for entry in os.listdir(sites_root) if os.path.isdir(sites_root) else []:
        writer.remove(f"sites/{entry}/index.html")


def render_static_pages(writer: PageWriter, env: Environment, common: dict):
    """About page, user guide, privacy notice (its text follows which analytics IDs are configured)."""
    for page in ("about.html", "guide.html", "privacy.html"):
        writer.page(page, env.get_template(page).render(**common))


def render_archive(writer: PageWriter, env: Environment, months: list, weeks: list, common: dict):
    """Archive: everything older than the timeline, by week and by month."""
    writer.page("archive/index.html", env.get_template("archive_index.html").render(months=months, weeks=weeks, **common))
    for i, week in enumerate(weeks):
        writer.page(f"archive/{week['key']}.html", env.get_template("archive_week.html").render(
            period=week, newer=weeks[i - 1] if i > 0 else None, older=weeks[i + 1] if i + 1 < len(weeks) else None, **common))
    for i, month in enumerate(months):
        writer.page(f"archive/{month['key']}.html", env.get_template("archive_month.html").render(
            period=month, days=group_by_day(month["updates"]), newer=months[i - 1] if i > 0 else None,
            older=months[i + 1] if i + 1 < len(months) else None, **common))


def write_feeds(writer: PageWriter, sites: list, tags: list, updates: list, tag_groups: list, keep_generated: bool):
    """search.json, feeds/status.json and the Atom feeds (all sites, per site, per tag)."""
    base_url = os.environ.get("SITE_BASE_URL", SITE_BASE_URL)
    names = {s["slug"]: s["name"] for s in sites}
    site_by_slug = {s["slug"]: s for s in sites}
    # Full-text search index (loaded by assets/app.js when the visitor starts searching)
    writer.page("search.json", build_search_index(sites, updates, tag_groups))
    # feeds/status.json: what the client apps poll (one small file instead of one feed per followed site).
    # A rebuild without a run (--no-push) keeps the old time: "generated" tells a client that a RUN has finished.
    status_path = writer.path("feeds", "status.json")
    generated_utc = (previous_generated(status_path) if keep_generated else None) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    writer.write(status_path, build_status(sites, generated_utc))
    writer.page("feeds/all.xml",
                build_atom("G醫t 更新情報（すべて）", "feeds/all.xml", "", updates[:FEED_LIMIT_ALL], names, base_url))
    for site in sites:
        writer.page(f"feeds/{site['slug']}.xml",
                    build_atom(f"G醫t {site['name']}", site["feed"], f"sites/{site['slug']}/{SITE_PAGE}",
                               site["updates"][:FEED_LIMIT_SITE], names, base_url))
    for tag in tags:
        tagged = [u for u in updates if tag["name"] in site_by_slug.get(u.get("site_slug"), {}).get("tags", [])]
        writer.page(f"feeds/{tag['id']}.xml",
                    build_atom(f"G醫t 分類: {tag['name']}", tag["feed"], "sites.html", tagged[:FEED_LIMIT_SITE], names, base_url))


def write_sitemap(writer: PageWriter, sites: list, months: list, weeks: list, updates: list):
    """sitemap.xml and robots.txt: only for a site of its own (see canonical_base); otherwise remove stale copies."""
    canon = writer.base
    if not canon:
        writer.remove("sitemap.xml")
        writer.remove("robots.txt")
        return
    writer.page("sitemap.xml", build_sitemap(canon, sites, months, weeks, updates))
    writer.page("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {canon}sitemap.xml\n")


# ---------------------------------------------------------------- main

def parse_args():
    parser = argparse.ArgumentParser(description="Build static dashboard for G-I-T")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    parser.add_argument("--no-push", action="store_true",
                        help="only write public/: no commit, no push (also DASHBOARD_NO_PUSH=1); for a rebuild after a change of templates or scripts")
    parser.add_argument("--migrate-diff-pages", action="store_true",
                        help="work on ALL diff pages (decorate, share style and script, canonical link); by default only the pages "
                             "of this run are made and every finished page is left as it is (also MIGRATE_DIFF_PAGES=1)")
    args = parser.parse_args()
    args.migrate_diff_pages = args.migrate_diff_pages or env_flag("MIGRATE_DIFF_PAGES")
    args.no_push = args.no_push or env_flag("DASHBOARD_NO_PUSH")
    return args


def main():
    args = parse_args()
    if done_file() and os.path.isfile(done_file()):
        os.remove(done_file())  # never a leftover of an earlier build

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    templates_dir = os.path.join(app_dir, "templates")
    static_dir = os.path.join(app_dir, "static")
    public_dir = os.path.join(args.data_dir, "public")

    # Fetch updates first: on failure keep the previously published dashboard
    updates = fetch_updates_from_supabase()
    if updates is None:
        print("Could not fetch updates; leaving public/ untouched.")
        if not args.no_push:
            commit_and_push_parent(args.data_dir)  # still publish new diff pages
        sys.exit(1)
    print(f"Rewrote {normalize_legacy_summaries(updates)} first-snapshot summar(ies) from the old text.")

    os.makedirs(public_dir, exist_ok=True)
    base = canonical_base()
    writer = PageWriter(public_dir, base)
    configured = configured_sites(args.data_dir)
    version = asset_version(static_dir) if os.path.isdir(static_dir) else ""
    prepare_diff_pages(public_dir, configured, updates, base, args.migrate_diff_pages, version)

    sites = build_site_infos(configured, updates)
    env = make_env(templates_dir, sites, version)
    update_old_diff_pages(public_dir, {s["slug"]: s["name"] for s in sites}, updates, base, args.migrate_diff_pages)

    tag_groups = configured_tag_groups(args.data_dir)
    tags = build_tag_infos(sites, tag_groups)
    common = dict(generated=to_jst(datetime.now(timezone.utc)), sites=sites, tags=tags, total_updates=len(updates))

    # Stylesheets, scripts, icons ... -> assets/. static/root/ holds the files that must live at the site root.
    if os.path.isdir(static_dir):
        shutil.copytree(static_dir, os.path.join(public_dir, "assets"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("root"))
        build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # makes every deployment a new service worker
        publish_root_files(writer, os.path.join(static_dir, "root"), build_id, version, env.globals["pwa_mode"])
    writer.page("offline.html", env.get_template("offline.html").render())

    render_timelines(writer, env, updates, common)
    render_site_pages(writer, env, sites, common)
    render_static_pages(writer, env, common)
    months, weeks = group_periods(updates)
    render_archive(writer, env, months, weeks, common)
    write_feeds(writer, sites, tags, updates, tag_groups, keep_generated=args.no_push)
    write_sitemap(writer, sites, months, weeks, updates)

    if done_file():  # every page is written: the result can be deployed, whatever happens to the commit and push below
        with open(done_file(), "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + chr(10))

    if args.no_push:
        print("--no-push: public/ is written, nothing is committed or pushed.")
    elif not commit_and_push_parent(args.data_dir):
        sys.exit(1)
    if config_problems():
        print("config.yaml could not be read; the dashboard was built from the last readable version. Failing the run so that it gets fixed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
