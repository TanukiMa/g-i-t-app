"""The pipeline: provision, fetch every site (one commit per changed site), push once, log the updates, queue them for the
Internet Archive, summarize them, and build the dashboard. See AGENTS.md section 4.3 for the order and its reasons.

The parts live in their own modules: diffcore (what a diff really changes), summarize (AI), links (what is archived),
db (Supabase), datarepo (patches), budget (the run's time limits); this file only decides what happens to one site and in
which order the run goes."""
import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
import yaml

import check_config
import json_records
import provision
from budget import FETCH_RESERVE_MIN, SUMMARY_RESERVE_MIN, run_deadline
from common import SUMMARY_FAILED, SUMMARY_INITIAL, SUMMARY_UNAVAILABLE
from datarepo import write_diff_patch
from db import enqueue_archives, get_supabase, log_to_supabase, set_summary, write_summary
from diffcore import analyze_diff, visible_len
from links import attachments_for
from shell import git, run_cmd
from summarize import summarize_diff_with_gemini

SUMMARY_WORKERS = max(1, int(os.environ.get("SUMMARY_WORKERS") or 4))    # summaries written in parallel
BACKFILL_LIMIT = int(os.environ.get("SUMMARY_BACKFILL_LIMIT", "10"))

# A server (or the CDN in front of it) sometimes answers 200 with a page whose body is empty. Such a page is not an
# update: it is fetched again BLANK_RETRIES times, and a page that is still blank is not recorded the first time
# (a marker file says so; a second blank run in a row is recorded, the page may really have been emptied).
BLANK_RETRIES = int(os.environ.get("BLANK_RETRIES", "2"))
BLANK_RETRY_WAIT_SEC = float(os.environ.get("BLANK_RETRY_WAIT_SEC", "30"))
BLANK_MARKER = ".suspect-blank"
BLANK_MIN_OLD_CHARS = 500    # the stored page had at least this much visible text ...
BLANK_MAX_RATIO = 50         # ... and the new one has less than 1/50 of it (and under BLANK_MAX_NEW_CHARS)
BLANK_MAX_NEW_CHARS = 200

# CI runners have no git identity; commits would fail without one.
os.environ.setdefault("GIT_AUTHOR_NAME", "g-i-t-bot")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "g-i-t-bot@users.noreply.github.com")
os.environ.setdefault("GIT_COMMITTER_NAME", os.environ["GIT_AUTHOR_NAME"])
os.environ.setdefault("GIT_COMMITTER_EMAIL", os.environ["GIT_AUTHOR_EMAIL"])


# ---------------------------------------------------------------- what the pipeline needs to know about a site

@dataclass
class SiteOptions:
    """The G-I-T keys of a site in config.yaml that change how the pipeline treats it (never written to
    website-stalker.yaml). The defaults are those of a site that config.yaml does not mention (any more)."""
    display_url: str = ""          # shown to readers instead of the fetched address ("" = the fetched one)
    records_spec: dict = None      # json_records: store a JSON data file one record per line
    paused: bool = False           # not fetched at all
    archive: bool = True           # archive: false = nothing of the site goes into archive_queue
    archive_links: bool = True     # archive_links: false = only the page and its documents are queued, not its new links
    report_reorder: bool = False   # a change that only reorders lines is reported as an update

    @classmethod
    def from_site(cls, site: dict) -> "SiteOptions":
        """From one entry of provision.configured_sites()."""
        return cls(display_url=site["url"] if site["url"] != site["fetch_url"] else "",
                   records_spec=site.get("json_records"), paused=bool(site.get("paused")),
                   archive=site.get("archive", True), archive_links=site.get("archive_links", True),
                   report_reorder=bool(site.get("report_reorder")))


def load_site_options(data_dir: str) -> dict:
    """{slug: SiteOptions} for every site of config.yaml ({} when it cannot be read: every site gets the defaults)."""
    try:
        return {s["slug"]: SiteOptions.from_site(s) for s in provision.configured_sites(data_dir)}
    except Exception as e:
        print(f"display_url / json_records lookup skipped: {e}")
        return {}


@dataclass
class Pending:
    """An `Update <slug>` commit of this run, on its way into the database."""
    row: dict                      # the columns of `updates`; row["summary"] None = still to be summarized
    fetch_url: str                 # what was fetched: the address queued for the Internet Archive
    raw_diff: str = None           # None for a first snapshot (nothing to summarize)
    attachments: list = field(default_factory=list)
    row_id: object = None
    summary_model: str = None


# ---------------------------------------------------------------- asking the server ourselves

def _request_page(url: str, headers: list):
    """One plain GET with the site's headers (and the From address), as website-stalker would send it."""
    sent = {k.strip(): v.strip() for k, _, v in (h.partition(":") for h in headers if ":" in h)}
    if os.environ.get("WEBSITE_STALKER_FROM"):
        sent.setdefault("From", os.environ["WEBSITE_STALKER_FROM"])
    return requests.get(url, headers=sent, timeout=20)


def describe_response(url: str, headers: list) -> str:
    """For the log: what does the server answer right now?"""
    try:
        r = _request_page(url, headers)
        keep = {k: r.headers[k] for k in ("Server", "Content-Type", "Content-Length", "X-Cache", "Via", "Age", "Cache-Control")
                if k in r.headers}
        return f"HTTP {r.status_code}, {len(r.content)} bytes, visible text {visible_len(r.text)} chars, {keep}"
    except Exception as e:
        return f"diagnostic request failed: {e}"


def server_gives_no_page(url: str, headers: list) -> str:
    """Asks the server once more, with the site's headers. '' when it answers HTTP 200 with a body (or when the
    question cannot be answered); otherwise what it says instead: 'HTTP 202', 'an empty body'. A CDN or its bot
    protection sometimes answers 202 with 0 bytes (meti.go.jp, from Cloud Run, for hours): that is a failed fetch, not a
    page that was emptied, and website-stalker stores it as if it were the page."""
    try:
        r = _request_page(url, headers)
    except Exception:
        return ""
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if not r.content:
        return "an empty body"
    return ""


# ---------------------------------------------------------------- one site

def read_site_target(config_file: str):
    """(url, headers) of the site's website-stalker.yaml; ("", []) when it cannot be read."""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if isinstance(cfg, dict) and "sites" in cfg and len(cfg["sites"]) > 0:
            site = cfg["sites"][0]
            return site.get("url", ""), [h for h in (site.get("headers") or []) if isinstance(h, str)]
    except Exception as e:
        print(f"Error reading {config_file}: {e}")
    return "", []


def fetch_site(site_path: str, slug: str, records_spec=None):
    """website-stalker in the site's directory (reads ./website-stalker.yaml, writes the fetched pages next to it), then
    `json_records` (a JSON data file stored one record per line, before git looks for changes)."""
    run_cmd(["website-stalker", "run", "--all"], cwd=site_path)
    if records_spec:
        try:
            n = json_records.apply_to_site(site_path, records_spec)
            print(f"json_records: {n} file(s) stored one record per line.")
        except Exception as e:
            print(f"json_records failed for {slug}: {e}")


def blanked_files(data_dir: str, rel_site: str) -> list:
    """[(path, old, new)] for stored page files that were just rewritten to (almost) no visible text although the
    committed version had plenty: the signature of an empty answer from the server, not of an update."""
    found = []
    for path in git(["diff", "--name-only", "--", rel_site], data_dir).stdout.splitlines():
        path = path.strip()
        if not path or os.path.basename(path) in ("website-stalker.yaml", BLANK_MARKER):
            continue
        try:
            with open(os.path.join(data_dir, path), encoding="utf-8", errors="replace") as f:
                new_len = visible_len(f.read())
        except OSError:
            continue
        old_len = visible_len(git(["show", f"HEAD:{path}"], data_dir).stdout)
        if old_len >= BLANK_MIN_OLD_CHARS and new_len < BLANK_MAX_NEW_CHARS and new_len * BLANK_MAX_RATIO < old_len:
            found.append((path, old_len, new_len))
    return found


def settle_blank_answer(data_dir: str, slug: str, target_url: str, headers: list, refetch) -> bool:
    """An empty answer is not an update (see BLANK_RETRIES). Fetches again while the page is blank; then decides.
    Returns False when nothing may be recorded in this run (the working tree is restored), True to go on."""
    rel_site = f"sites/{slug}"
    blank = blanked_files(data_dir, rel_site)
    for attempt in range(1, BLANK_RETRIES + 1):
        if not blank:
            break
        _path, old_len, new_len = blank[0]
        print(f"{slug}: the page has {new_len} visible characters instead of {old_len} (an empty answer?). "
              f"Server now: {describe_response(target_url, headers)}")
        print(f"{slug}: fetching again in {BLANK_RETRY_WAIT_SEC:g} s ({attempt}/{BLANK_RETRIES}).")
        time.sleep(BLANK_RETRY_WAIT_SEC)
        refetch()
        blank = blanked_files(data_dir, rel_site)
    if blank:
        # The server itself says it is not giving the page (HTTP 202, no body ...): a failed fetch, never an update,
        # however many runs in a row it lasts. Nothing is committed; the stored page stays as it is.
        reason = server_gives_no_page(target_url, headers)
        if reason:
            print(f"{slug}: the server answers {reason} instead of the page: not recorded, the stored page stays as it is.")
            git(["checkout", "--", rel_site], data_dir)
            return False
    # The marker file (committed) remembers the suspicion until the next run.
    rel_marker = f"{rel_site}/{BLANK_MARKER}"
    suspected = os.path.exists(os.path.join(data_dir, rel_marker))
    if blank and not suspected:
        print(f"{slug}: still blank: not recorded this time (a second blank run in a row is recorded).")
        git(["checkout", "--", rel_site], data_dir)
        with open(os.path.join(data_dir, rel_marker), "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        git(["add", "--", rel_marker], data_dir)
        git(["commit", "-m", f"Suspect blank response of {slug} (not recorded as an update)", "--only", "--", rel_marker], data_dir)
        return False
    if suspected:
        print(f"{slug}: {'blank again, so it is recorded' if blank else 'the page is back to normal'}; "
              "clearing the suspicion.")
        git(["rm", "-q", "-f", "--", rel_marker], data_dir)
        git(["commit", "-m", f"Clear blank-response suspicion of {slug}", "--only", "--", rel_marker], data_dir)
    return True


def silent_commit_message(slug: str, analysis, is_initial: bool, rebaseline: bool, report_reorder: bool):
    """The message of a change that is committed but NOT reported (no summary, diff page, row or archive request), or None
    for a change that is an update.

    Re-baseline: the site's ignore/remove rules changed in this run, so the stored page changes shape once. A rule only
    deletes text or shortens an address: if the diff ADDS visible text, the page really changed in the same run and it is
    reported. Reorder: the same lines in a different order (swapped tabs, a shuffled list); the new order is still
    committed so that the stored page stays exact."""
    if is_initial:
        return None
    if rebaseline:
        added = analysis.added_text()
        if not added:
            print(f"Ignore rules of {slug} changed in this run: committing the new baseline, not reporting an update.")
            return f"Re-baseline {slug} after ignore rule change"
        print(f"Ignore rules of {slug} changed in this run, but the page also gained text ({len(added)} line(s), e.g. "
              f"{added[0][:50]!r}): reported as an update.")
    if not report_reorder and analysis.moved_only:
        print(f"{slug}: the same content in a different order; committed, not reported as an update.")
        return f"Reorder {slug} (same content, different order)"
    return None


def process_site(data_dir: str, site_slug: str, opts: SiteOptions = None, rebaseline: bool = False):
    """Stalk one site and commit its changes as one commit. Returns a Pending update or None.

    The record's url (stored in `updates`) is the site's display_url when it has one, while fetch_url (what is queued
    for the Internet Archive) stays the fetched address. With rebaseline=True (the site's ignore rules were just changed)
    the resulting change is normally committed without being reported (silent_commit_message).
    """
    opts = opts or SiteOptions()
    rel_site = f"sites/{site_slug}"
    site_path = os.path.join(data_dir, "sites", site_slug)
    config_file = os.path.join(site_path, "website-stalker.yaml")
    if not os.path.exists(config_file):
        print(f"Config {config_file} does not exist. Skipping.")
        return None
    target_url, site_headers = read_site_target(config_file)
    domain = urlparse(target_url).netloc if target_url else site_slug

    # First fetch of this site: nothing but the config is tracked yet, so there is no "change" to describe.
    tracked = git(["ls-files", "--", rel_site], data_dir).stdout.split()
    is_initial = all(os.path.basename(p) == "website-stalker.yaml" for p in tracked)

    print(f"--- Running website-stalker in {site_path} ---")
    fetch_site(site_path, site_slug, opts.records_spec)
    if not is_initial and not settle_blank_answer(data_dir, site_slug, target_url, site_headers,
                                                  lambda: fetch_site(site_path, site_slug, opts.records_spec)):
        return None

    if not git(["status", "--porcelain", "--", rel_site], data_dir).stdout.strip():
        print(f"No changes detected in {site_slug}.")
        return None
    git(["add", "-A", "--", rel_site], data_dir)
    raw_diff = git(["diff", "--cached", "--", rel_site], data_dir).stdout
    analysis = analyze_diff(raw_diff)         # one reading of the diff for the Re-baseline and the reorder check

    message = silent_commit_message(site_slug, analysis, is_initial, rebaseline, opts.report_reorder)
    if message:
        git(["commit", "-m", message, "--only", "--", rel_site], data_dir)
        return None

    # The AI summary is NOT made here: it does not change the commit, and waiting for the AI site by site is what made
    # runs slow. main() summarizes all changed sites in parallel after the push. (Not for the initial snapshot.)
    if is_initial:
        print(f"Initial snapshot of {site_slug}: skipping AI summary, diff page and attachment queueing.")

    # One commit per site per run; the hash is logged and never amended.
    if git(["commit", "-m", f"Update {site_slug}", "--only", "--", rel_site], data_dir).returncode != 0:
        print(f"Commit failed for {site_slug}; skipping.")
        return None
    commit_hash = git(["rev-parse", "HEAD"], data_dir).stdout.strip()

    # The patch of the commit goes into public/ (published by the dashboard commit); the page that shows it is made there
    if not is_initial and not write_diff_patch(data_dir, site_slug, commit_hash):
        print(f"Failed to write the patch of {site_slug} ({commit_hash[:7]}).")

    page_url = target_url or f"https://{domain}"
    return Pending(
        row={"site_slug": site_slug, "domain": domain, "url": opts.display_url or page_url, "commit_hash": commit_hash,
             "summary": SUMMARY_INITIAL if is_initial else None},
        fetch_url=page_url,
        raw_diff=None if is_initial else raw_diff,
        attachments=[] if is_initial else attachments_for(raw_diff, analysis, page_url, opts.archive_links),
    )


# ---------------------------------------------------------------- the steps of a run

def provision_sites(data_dir: str) -> set:
    """Step 1 (local only: creates sites/<slug>/website-stalker.yaml). Returns the sites whose ignore rules changed in this
    run: their stored pages change shape once (e.g. cache-busting numbers disappear), which is not an update."""
    print("=== Step 1: JIT Auto-Provisioning ===")
    try:
        prov = provision.provision_all(data_dir)
    except Exception as e:
        print(f"Provisioning failed: {e}")
        prov = {"new": [], "rules_changed": []}
    return set(prov["rules_changed"])


def stalk_sites(data_dir: str, options: dict, rebaseline: set) -> list:
    """Step 2: one commit per changed site, as long as the run budget allows. Returns the Pending updates."""
    print("=== Step 2: Stalk & Process Sites ===")
    paused = {slug for slug, o in options.items() if o.paused}
    if paused:
        print(f"Paused (`paused: true` in config.yaml, not fetched): {', '.join(sorted(paused))}")
    sites_dir = os.path.join(data_dir, "sites")
    if not os.path.exists(sites_dir):
        return []
    # The directories of sites/, not config.yaml: a site removed from config.yaml keeps being fetched (use `paused`).
    entries = [e for e in sorted(os.listdir(sites_dir)) if os.path.isdir(os.path.join(sites_dir, e)) and e not in paused]
    pending = []
    for done, entry in enumerate(entries):
        if time.monotonic() >= run_deadline(FETCH_RESERVE_MIN):
            print(f"Run time budget used up: {len(entries) - done} site(s) are left for the next run.")
            break
        try:
            record = process_site(data_dir, entry, options.get(entry), rebaseline=entry in rebaseline)
            if record:
                pending.append(record)
        except Exception as e:
            print(f"Error processing site {entry}: {e}")
    return pending


def record_updates(pending: list, options: dict):
    """Step 4: the rows are stored FIRST (a summary still to come is marked SUMMARY_FAILED), so a run that dies or is cut
    off during the AI part has lost nothing: backfill_summaries() fills the rest in later runs. Then the archive queue."""
    for rec in pending:
        todo = rec.row["summary"] is None
        rec.row_id = log_to_supabase({**rec.row, "summary": SUMMARY_FAILED} if todo else dict(rec.row))
        slug = rec.row["site_slug"]
        if not options.get(slug, SiteOptions()).archive:
            print(f"Not queued for archiving (`archive: false`): {slug}")
        else:
            enqueue_archives(slug, rec.row["commit_hash"], rec.fetch_url, rec.attachments)


def summarize_updates(pending: list):
    """Step 4a: summaries of the changed sites, several at a time, written into their rows as they arrive."""
    todo = [r for r in pending if r.row["summary"] is None]
    if not todo:
        return
    print(f"=== Step 4a: AI summaries for {len(todo)} site(s), {SUMMARY_WORKERS} at a time ===")
    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        futures = {pool.submit(summarize_diff_with_gemini, r.raw_diff): r for r in todo}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                rec.row["summary"], rec.summary_model = fut.result()
            except Exception as e:
                print(f"Summary of {rec.row['site_slug']} failed: {e}")
                rec.row["summary"], rec.summary_model = SUMMARY_FAILED, None
            if rec.row["summary"] != SUMMARY_FAILED:
                # The row could not be stored in step 4: store it now, with its summary.
                if not set_summary(rec.row_id, rec.row["summary"], rec.summary_model) and rec.row_id is None:
                    log_to_supabase(dict(rec.row))
            print(f"Summarized {rec.row['site_slug']} ({rec.row['commit_hash'][:7]}).")


def backfill_summaries(data_dir: str):
    """Step 4b: regenerate summaries that failed earlier (e.g. Gemini 503), newest first, a few per run (needs the full
    history: fetch-depth 0)."""
    if BACKFILL_LIMIT <= 0:
        return
    try:
        supabase = get_supabase()
        if not supabase:
            return
        rows = (supabase.table("updates").select("id,site_slug,commit_hash")
                .eq("summary", SUMMARY_FAILED).order("id", desc=True)
                .limit(BACKFILL_LIMIT).execute().data or [])
    except Exception as e:
        print(f"Error listing failed summaries: {e}")
        return
    print(f"{len(rows)} failed summar{'y' if len(rows) == 1 else 'ies'} to retry (limit {BACKFILL_LIMIT}).")

    for row in rows:
        if time.monotonic() >= run_deadline(SUMMARY_RESERVE_MIN):
            print("Run time budget used up; the remaining failed summaries wait for the next run.")
            return
        patch = git(["show", "--format=", "--patch", row["commit_hash"], "--", f"sites/{row['site_slug']}"], data_dir)
        if patch.returncode != 0 or not patch.stdout.strip():
            new_summary, new_model = SUMMARY_UNAVAILABLE, None  # commit gone or empty: stop retrying it
        else:
            try:
                new_summary, new_model = summarize_diff_with_gemini(patch.stdout)
            except Exception as e:
                print(f"Summary of update {row['id']} failed: {e}")
                new_summary, new_model = SUMMARY_FAILED, None
            if new_summary == SUMMARY_FAILED:
                print("Gemini still failing; stopping backfill for this run.")
                return
        try:
            write_summary(supabase, row["id"], new_summary, new_model)
            print(f"Updated summary of update {row['id']} ({row['site_slug']}).")
        except Exception as e:
            print(f"Error updating summary of update {row['id']}: {e}")


def build_dashboard(data_dir: str) -> int:
    """Step 5: build_dashboard.py in its own process (it also commits and pushes public/). Returns its exit status."""
    print("=== Step 5: Build Static Dashboard ===")
    build_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_dashboard.py")
    return subprocess.run([sys.executable, build_script, "--data-dir", data_dir]).returncode


def main():
    # Localized OS error messages / Japanese summaries must never crash logging.
    # line_buffering: the log must show progress while the run is going (a cancelled run would otherwise lose it).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="G-I-T Processing Pipeline")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    if not shutil.which("website-stalker"):
        print("website-stalker is not installed or not on PATH.")
        sys.exit(1)

    # Step 0: is config.yaml sound? Findings are printed; when the file cannot be read at all, provisioning and the
    # dashboard go on with the last readable committed version (provision.read_config) and the run fails at the end.
    config_errors = []
    try:
        config_errors, _ = check_config.report(args.data_dir)
    except Exception as e:
        print(f"config.yaml check skipped: {e}")

    rebaseline = provision_sites(args.data_dir)
    options = load_site_options(args.data_dir)
    pending = stalk_sites(args.data_dir, options, rebaseline)

    # Step 3: Publish all site commits (and provisioning commits) with one push. Only commits that are on GitHub are
    # logged and summarized.
    pushed = git(["push", "origin", "HEAD"], args.data_dir).returncode == 0
    if pushed:
        record_updates(pending, options)
        summarize_updates(pending)
    else:
        print("Push failed; not logging this run's updates to Supabase.")

    backfill_summaries(args.data_dir)
    build = build_dashboard(args.data_dir)
    config_failed = bool(config_errors or provision.config_problems())
    if config_failed:
        print("config.yaml has errors (see above): the run is reported as failed so that it gets fixed.")
    sys.exit(0 if build == 0 and pushed and not config_failed else 1)


if __name__ == "__main__":
    main()
