import os
import sys
import argparse
import shutil
import subprocess
from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

PAGE_SIZE = 1000


def attach_diff_pages(updates, data_dir: str, public_dir: str):
    """Copy each update's diff_<hash7>.html next to the site page and set item['diff_file']."""
    for item in updates:
        slug, commit = item.get("site_slug"), item.get("commit_hash") or ""
        if not slug or not commit:
            continue
        name = f"diff_{commit[:7]}.html"
        src = os.path.join(data_dir, "sites", slug, name)
        if not os.path.isfile(src):
            continue
        dest_dir = os.path.join(public_dir, "sites", slug)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(src, os.path.join(dest_dir, name))
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


def git(args, cwd):
    print(f"Executing in {cwd}: git {' '.join(args)}")
    res = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        print(f"git {args[0]} failed ({res.returncode}): {res.stderr}")
    return res


def commit_and_push_parent(data_dir: str) -> bool:
    """Update submodule pointers, commit the global snapshot and push g-i-t-data."""
    git(["submodule", "update", "--remote"], data_dir)
    git(["add", "-A"], data_dir)
    if git(["diff", "--cached", "--quiet"], data_dir).returncode == 0:
        print("No parent repository changes to commit.")
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if git(["commit", "-m", f"Snapshot {stamp}"], data_dir).returncode != 0:
        return False
    return git(["push", "origin", "HEAD"], data_dir).returncode == 0

def main():
    parser = argparse.ArgumentParser(description="Build static dashboard for G-I-T")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    templates_dir = os.path.join(app_dir, "templates")
    public_dir = os.path.join(args.data_dir, "public")

    # Fetch updates first: on failure keep the previously published dashboard
    updates = fetch_updates_from_supabase()
    if updates is None:
        print("Could not fetch updates; leaving public/ untouched.")
        commit_and_push_parent(args.data_dir)  # still persist submodule pointers
        sys.exit(1)

    os.makedirs(public_dir, exist_ok=True)

    attach_diff_pages(updates, args.data_dir, public_dir)

    # Summaries derive from untrusted web content, so escape everything.
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html"]))
    index_template = env.get_template("index.html")
    site_detail_template = env.get_template("site_detail.html")

    # Render main index.html
    rendered_index = index_template.render(updates=updates)
    with open(os.path.join(public_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(rendered_index)
    print(f"Generated {os.path.join(public_dir, 'index.html')}")

    # Group updates by site_slug for site detail pages
    sites_updates = {}
    for update in updates:
        slug = update.get("site_slug")
        if slug:
            sites_updates.setdefault(slug, []).append(update)

    for slug, site_upds in sites_updates.items():
        site_public_dir = os.path.join(public_dir, "sites", slug)
        os.makedirs(site_public_dir, exist_ok=True)
        rendered_site = site_detail_template.render(site_slug=slug, updates=site_upds)
        with open(os.path.join(site_public_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(rendered_site)
        print(f"Generated {os.path.join(site_public_dir, 'index.html')}")

    if not commit_and_push_parent(args.data_dir):
        sys.exit(1)

if __name__ == "__main__":
    main()
