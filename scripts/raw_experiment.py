"""Raw arm of the editor experiment: store every monitored page WITHOUT any website-stalker editor.

For each site of g-i-t-data/config.yaml a `sites/<slug>/website-stalker.yaml` with `editors: []` is written into a
separate repository (g-i-t-data-raw), website-stalker fetches the page, and every change becomes one commit.
The number and size of these commits, compared with the commits of g-i-t-data (editors on), shows how much
noise the editors remove (see scripts/compare_raw.py). No summaries, no database, no dashboard.

    RAW_EXPERIMENT=1 python scripts/raw_experiment.py --data-dir ../g-i-t-data --raw-dir ../g-i-t-data-raw
"""
import argparse
import concurrent.futures
import os
import subprocess
import sys
import time

import yaml

import provision

WORKERS = int(os.environ.get("RAW_WORKERS", "4"))
BUDGET_MIN = float(os.environ.get("RAW_BUDGET_MIN", "40"))
# Never in the raw file: G-I-T metadata (RESERVED_KEYS) and the editors, which are the thing under test.
HEADERS = provision.DEFAULT_SITE_OPTIONS["headers"]


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def raw_config(target) -> dict:
    """Same URL and headers as the monitored site, no editors."""
    url = target.get("url") if isinstance(target, dict) else str(target)
    extra = {k: v for k, v in target.items() if k not in provision.RESERVED_KEYS} if isinstance(target, dict) else {}
    return {"url": url, "headers": HEADERS, **extra, "editors": []}


def write_config(raw_dir: str, slug: str, cfg: dict) -> None:
    site_dir = os.path.join(raw_dir, "sites", slug)
    path = os.path.join(site_dir, "website-stalker.yaml")
    wanted = {"sites": [cfg]}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                if yaml.safe_load(f) == wanted:
                    return
        except yaml.YAMLError:
            pass
    os.makedirs(site_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(wanted, f, allow_unicode=True, sort_keys=False)


def fetch(raw_dir: str, slug: str, deadline: float) -> bool:
    if time.time() > deadline:
        return False
    site_dir = os.path.join(raw_dir, "sites", slug)
    res = provision.run_cmd(["website-stalker", "run", "--all"], cwd=site_dir)
    return res.returncode == 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Raw arm (no editors) of the editor experiment")
    parser.add_argument("--data-dir", default="./data", help="g-i-t-data checkout (reads config.yaml)")
    parser.add_argument("--raw-dir", default="./raw", help="g-i-t-data-raw checkout (a git repository)")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    # A switch, so that a scheduled job costs nothing while the experiment is not running (RAW_EXPERIMENT=1 turns it on).
    if os.environ.get("RAW_EXPERIMENT", "").strip().lower() not in ("1", "true", "yes", "on"):
        print("RAW_EXPERIMENT is not set to 1: the raw arm is off, nothing to do.")
        return
    if not os.environ.get("WEBSITE_STALKER_FROM"):
        print("WEBSITE_STALKER_FROM is not set.")
        sys.exit(1)
    if git(["rev-parse", "--git-dir"], args.raw_dir).returncode != 0:
        print(f"{args.raw_dir} is not a git repository.")
        sys.exit(1)

    deadline = time.time() + BUDGET_MIN * 60
    slugs = []
    for target in provision.parse_stalker_yaml(args.data_dir):
        slug = provision.target_slug(target)
        url = target.get("url") if isinstance(target, dict) else str(target)
        if not url or not provision.SLUG_RE.match(slug) or slug in slugs:
            continue
        write_config(args.raw_dir, slug, raw_config(target))
        slugs.append(slug)
    print(f"=== Raw arm: {len(slugs)} sites ===")

    # Fetching is slow and independent per site (parallel); git is not (one commit per site, in this thread).
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {slug: pool.submit(fetch, args.raw_dir, slug, deadline) for slug in slugs}
    failed, skipped, committed = [], [], 0
    for slug in slugs:
        if not futures[slug].result():
            (skipped if time.time() > deadline else failed).append(slug)
            continue
        path = f"sites/{slug}"
        if not git(["status", "--porcelain", "--", path], args.raw_dir).stdout.strip():
            continue
        first = not git(["ls-files", "--", path], args.raw_dir).stdout.strip()
        git(["add", "-A", "--", path], args.raw_dir)
        msg = f"Baseline {slug}" if first else f"Raw {slug}"
        res = git(["commit", "-m", msg, "--", path], args.raw_dir)
        if res.returncode == 0:
            committed += 1
        else:
            print(f"commit failed for {slug}: {res.stdout}{res.stderr}")
            failed.append(slug)
    print(f"committed {committed}, fetch failed {len(failed)}, not started (budget) {len(skipped)}")

    if committed and not args.no_push:
        res = git(["push", "origin", "HEAD"], args.raw_dir)
        print(res.stdout + res.stderr)
        if res.returncode != 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
