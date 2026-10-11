"""Raw arm of the editor experiment: store every monitored page WITHOUT any website-stalker editor.

For each site of g-i-t-data/config.yaml a `sites/<slug>/website-stalker.yaml` with `editors: []` is written into a
separate repository (g-i-t-data-raw), website-stalker fetches the page, and every change becomes one commit.
The number and size of these commits, compared with the commits of g-i-t-data (editors on), shows how much
noise the editors remove (see scripts/compare_raw.py). No summaries, no database, no dashboard.

    RAW_EXPERIMENT=1 python scripts/raw_experiment.py --data-dir ../g-i-t-data --raw-dir ../g-i-t-data-raw

The experiment stops by itself. The state lives in the raw repository (`experiment.json`: the start time and the number
of runs, committed as `Experiment run N` at the end of every run, whether or not a page changed), so a scheduled job
that was not deleted cannot go on for ever: a run does nothing once RAW_DAYS (default 30) days have passed since the first
run, or RAW_RUNS_PER_DAY (default 1) x RAW_DAYS runs were made. RAW_SITES (comma separated slugs) limits the experiment
to some sites (default: all). To start a new experiment, delete `experiment.json` (or use a new repository).
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

import provision

WORKERS = int(os.environ.get("RAW_WORKERS", "4"))
BUDGET_MIN = float(os.environ.get("RAW_BUDGET_MIN", "40"))
DAYS = float(os.environ.get("RAW_DAYS", "30"))
RUNS_PER_DAY = float(os.environ.get("RAW_RUNS_PER_DAY", "1"))
STATE_FILE = "experiment.json"
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


def read_state(raw_dir: str) -> dict:
    """{"started": ISO time (UTC), "runs": n}; {} before the first run (or when the file cannot be read)."""
    try:
        with open(os.path.join(raw_dir, STATE_FILE), encoding="utf-8") as f:
            state = json.load(f)
        return {"started": str(state["started"]), "runs": int(state["runs"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def finished(state: dict, now: datetime, days: float = DAYS, runs_per_day: float = RUNS_PER_DAY) -> str:
    """Why the experiment is over ("" while it goes on): the period or the number of runs is used up."""
    if not state:
        return ""
    max_runs = int(days * runs_per_day)
    if state["runs"] >= max_runs:
        return f"{state['runs']} run(s) made, the limit is {max_runs} ({days:g} days x {runs_per_day:g} per day)"
    started = datetime.fromisoformat(state["started"])
    if now >= started + timedelta(days=days):
        return f"{days:g} days have passed since the first run ({started:%Y-%m-%d})"
    return ""


def record_run(raw_dir: str, state: dict, now: datetime) -> int:
    """Count this run in experiment.json and commit it (pushed with the rest). Returns the number of the run."""
    number = state.get("runs", 0) + 1
    new = {"started": state.get("started") or now.isoformat(timespec="seconds"), "runs": number}
    with open(os.path.join(raw_dir, STATE_FILE), "w", encoding="utf-8", newline="\n") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    git(["add", "--", STATE_FILE], raw_dir)
    res = git(["commit", "-m", f"Experiment run {number}", "--", STATE_FILE], raw_dir)
    if res.returncode != 0:
        print(f"could not record the run: {res.stdout}{res.stderr}")
    return number


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

    now = datetime.now(timezone.utc)
    state = read_state(args.raw_dir)
    over = finished(state, now)
    if over:
        print(f"The raw experiment is over ({over}); nothing to do. Delete the Cloud Run job and its scheduler entry.")
        return

    only = {s.strip() for s in os.environ.get("RAW_SITES", "").split(",") if s.strip()}
    deadline = time.time() + BUDGET_MIN * 60
    slugs = []
    for target in provision.parse_stalker_yaml(args.data_dir):
        slug = provision.target_slug(target)
        if only and slug not in only:
            continue
        url = target.get("url") if isinstance(target, dict) else str(target)
        if not url or not provision.SLUG_RE.match(slug) or slug in slugs:
            continue
        write_config(args.raw_dir, slug, raw_config(target))
        slugs.append(slug)
    missing = sorted(only - set(slugs))
    if missing:
        print(f"WARNING: RAW_SITES names sites that are not in config.yaml: {', '.join(missing)}")
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
    number = record_run(args.raw_dir, state, now)
    print(f"Run {number} of at most {int(DAYS * RUNS_PER_DAY)} ({DAYS:g} days from the first run).")

    if not args.no_push:
        res = git(["push", "origin", "HEAD"], args.raw_dir)
        print(res.stdout + res.stderr)
        if res.returncode != 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
