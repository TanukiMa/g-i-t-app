import os
import sys
import shutil
import time
import subprocess
import argparse
from urllib.parse import urljoin, urlparse
import yaml
from bs4 import BeautifulSoup

try:
    from google import genai
except ImportError:
    genai = None

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

# Override without code changes when Google retires a model for new users.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# Overload (503) and rate limits (429) are usually short-lived: retry with backoff.
GEMINI_TRANSIENT_CODES = (429, 500, 503, 504)
GEMINI_RETRY_WAITS = [10, 30, 60]

# Stored in updates.summary when no summary could be produced. SUMMARY_FAILED rows are retried
# by backfill_summaries(); SUMMARY_UNAVAILABLE marks rows whose diff can no longer be read.
SUMMARY_FAILED = "AI要約を生成できませんでした。"
SUMMARY_UNAVAILABLE = "差分を取得できなかったため要約できません。"
BACKFILL_LIMIT = int(os.environ.get("SUMMARY_BACKFILL_LIMIT", "10"))

# CI runners have no git identity; commits would fail without one.
os.environ.setdefault("GIT_AUTHOR_NAME", "g-i-t-bot")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "g-i-t-bot@users.noreply.github.com")
os.environ.setdefault("GIT_COMMITTER_NAME", os.environ["GIT_AUTHOR_NAME"])
os.environ.setdefault("GIT_COMMITTER_EMAIL", os.environ["GIT_AUTHOR_EMAIL"])


def run_cmd(cmd, cwd=None, input_text=None):
    print(f"Executing in {cwd or '.'}: {' '.join(cmd)}")
    res = subprocess.run(
        cmd, cwd=cwd, input=input_text, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        print(f"Command failed code {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    return res


def summarize_diff_with_gemini(diff_text: str) -> str:
    if not diff_text.strip():
        return "更新差分はありませんでした。"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not genai:
        print("GEMINI_API_KEY is not set or google-generativeai module is missing. Skipping AI summarization.")
        return "Gemini APIキー未設定のため自動要約はスキップされました。"

    prompt = (
        "以下のテキストはWebサイトのHTML更新差分(Git Diff)です。\n"
        "非エンジニア向けに、何が変更されたかを自然な日本語で箇条書き要約してください。\n"
        "システムコードやタグは無視し、意味のあるコンテンツの変更のみ抽出してください。\n\n"
        f"```diff\n{diff_text[:10000]}\n```"
    )
    client = genai.Client(api_key=api_key)
    for attempt, wait in enumerate(GEMINI_RETRY_WAITS + [None]):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text.strip()
        except Exception as e:
            # Details go to the log only; the summary is published on the dashboard.
            print(f"Error during Gemini API call (attempt {attempt + 1}): {e}")
            transient = getattr(e, "code", None) in GEMINI_TRANSIENT_CODES
            if wait is None or not transient:
                return SUMMARY_FAILED
            time.sleep(wait)
    return SUMMARY_FAILED


ATTACHMENT_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
MAX_ATTACHMENTS_PER_UPDATE = 50


def _host(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def extract_attachment_links(diff_text: str, base_url: str) -> list:
    """Same-domain document links found in the lines added by this diff."""
    added = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    base_host = _host(urlparse(base_url).netloc)
    links = []
    for a in BeautifulSoup(added, "html.parser").find_all("a", href=True):
        full = urljoin(base_url, a["href"].strip()).split("#")[0]
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https") or _host(parsed.netloc) != base_host:
            continue
        if not parsed.path.lower().endswith(ATTACHMENT_EXTS):
            continue
        if full not in links:
            links.append(full)
    return links[:MAX_ATTACHMENTS_PER_UPDATE]


def get_supabase():
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_KEY")
    if not create_client or not supa_url or not supa_key:
        print("Supabase URL/Key or supabase module missing. Skipping DB access.")
        return None
    return create_client(supa_url, supa_key)


def log_to_supabase(data_dict: dict):
    try:
        supabase = get_supabase()
        if supabase:
            res = supabase.table("updates").insert(data_dict).execute()
            print(f"Logged to Supabase: {res}")
    except Exception as e:
        print(f"Error inserting into Supabase: {e}")


def enqueue_archives(site_slug: str, commit_hash: str, page_url: str, attachment_urls: list):
    """Queue URLs for archive_worker.py; the Wayback calls happen there, not here."""
    rows = [{"site_slug": site_slug, "commit_hash": commit_hash, "url": page_url, "kind": "page"}]
    rows += [{"site_slug": site_slug, "commit_hash": commit_hash, "url": u, "kind": "attachment"}
             for u in attachment_urls]
    try:
        supabase = get_supabase()
        if supabase:
            supabase.table("archive_queue").upsert(
                rows, on_conflict="site_slug,commit_hash,url", ignore_duplicates=True
            ).execute()
            print(f"Queued {len(rows)} URL(s) for archiving.")
    except Exception as e:
        print(f"Error queueing archive URLs: {e}")


def git(args, cwd, **kw):
    return run_cmd(["git"] + args, cwd=cwd, **kw)


def backfill_summaries(data_dir: str):
    """Regenerate summaries that failed earlier (e.g. Gemini 503), newest first, a few per run."""
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
        patch = git(["show", "--format=", "--patch", row["commit_hash"], "--", f"sites/{row['site_slug']}"], data_dir)
        if patch.returncode != 0 or not patch.stdout.strip():
            new_summary = SUMMARY_UNAVAILABLE  # commit gone or empty: stop retrying it
        else:
            new_summary = summarize_diff_with_gemini(patch.stdout)
            if new_summary == SUMMARY_FAILED:
                print("Gemini still failing; stopping backfill for this run.")
                return
        try:
            supabase.table("updates").update({"summary": new_summary}).eq("id", row["id"]).execute()
            print(f"Updated summary of update {row['id']} ({row['site_slug']}).")
        except Exception as e:
            print(f"Error updating summary of update {row['id']}: {e}")


def render_diff_html(commit_hash: str, data_dir: str, rel_site: str):
    """Return side-by-side diff HTML for this site's part of a commit, or None on failure."""
    patch = git(["show", "--format=", "--patch", commit_hash, "--", rel_site], data_dir)
    if patch.returncode != 0 or not patch.stdout.strip():
        return None
    try:
        html = run_cmd(
            ["diff2html", "-i", "stdin", "-o", "stdout", "-s", "side"],
            input_text=patch.stdout,
        )
    except OSError as e:
        print(f"diff2html not available: {e}")
        return None
    if html.returncode != 0 or not html.stdout.strip():
        return None
    return html.stdout


def process_site(data_dir: str, site_slug: str):
    """Stalk one site and commit its changes as one commit. Returns a pending log record or None."""
    rel_site = f"sites/{site_slug}"
    site_path = os.path.join(data_dir, "sites", site_slug)
    config_file = os.path.join(site_path, "website-stalker.yaml")

    if not os.path.exists(config_file):
        print(f"Config {config_file} does not exist. Skipping.")
        return None

    # Extract target URL from config
    target_url = ""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            if isinstance(cfg, dict) and "sites" in cfg and len(cfg["sites"]) > 0:
                target_url = cfg["sites"][0].get("url", "")
    except Exception as e:
        print(f"Error reading {config_file}: {e}")

    domain = urlparse(target_url).netloc if target_url else site_slug

    # 1. Execute website-stalker (reads ./website-stalker.yaml, writes fetched pages next to it)
    print(f"--- Running website-stalker in {site_path} ---")
    run_cmd(["website-stalker", "run", "--all"], cwd=site_path)

    # 2. Any change inside this site's directory?
    status_res = git(["status", "--porcelain", "--", rel_site], data_dir)
    if not status_res.stdout.strip():
        print(f"No changes detected in {site_slug}.")
        return None

    git(["add", "-A", "--", rel_site], data_dir)
    raw_diff = git(["diff", "--cached", "--", rel_site], data_dir).stdout

    # 3. AI Summarization
    summary = summarize_diff_with_gemini(raw_diff)

    # One commit per site per run; the hash is logged and never amended.
    commit_res = git(["commit", "-m", f"Update {site_slug}", "--only", "--", rel_site], data_dir)
    if commit_res.returncode != 0:
        print(f"Commit failed for {site_slug}; skipping.")
        return None
    commit_hash = git(["rev-parse", "HEAD"], data_dir).stdout.strip()

    # 4. Diff HTML goes straight into public/ (published by the dashboard commit)
    diff_html = render_diff_html(commit_hash, data_dir, rel_site)
    if diff_html:
        diff_dir = os.path.join(data_dir, "public", "sites", site_slug)
        os.makedirs(diff_dir, exist_ok=True)
        with open(os.path.join(diff_dir, f"diff_{commit_hash[:7]}.html"), "w", encoding="utf-8") as f:
            f.write(diff_html)
    else:
        print(f"Failed to generate diff HTML for {site_slug}.")

    page_url = target_url or f"https://{domain}"
    return {
        "site_slug": site_slug,
        "domain": domain,
        "url": page_url,
        "commit_hash": commit_hash,
        "summary": summary,
        "attachments": extract_attachment_links(raw_diff, page_url),
    }


def main():
    # Localized OS error messages / Japanese summaries must never crash logging.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="G-I-T Processing Pipeline")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    if not shutil.which("website-stalker"):
        print("website-stalker is not installed or not on PATH.")
        sys.exit(1)

    # Step 1: Run Provisioning (local only: creates sites/<slug>/website-stalker.yaml)
    print("=== Step 1: JIT Auto-Provisioning ===")
    provision_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision.py")
    prov = subprocess.run([sys.executable, provision_script, "--data-dir", args.data_dir])
    if prov.returncode != 0:
        print(f"Provisioning exited with {prov.returncode}")

    # Step 2: One commit per changed site
    print("=== Step 2: Stalk & Process Sites ===")
    pending = []
    sites_dir = os.path.join(args.data_dir, "sites")
    if os.path.exists(sites_dir):
        for entry in sorted(os.listdir(sites_dir)):
            if os.path.isdir(os.path.join(sites_dir, entry)):
                try:
                    record = process_site(args.data_dir, entry)
                    if record:
                        pending.append(record)
                except Exception as e:
                    print(f"Error processing site {entry}: {e}")

    # Step 3: Publish all site commits (and provisioning commits) with one push
    pushed = git(["push", "origin", "HEAD"], args.data_dir).returncode == 0
    if not pushed:
        print("Push failed; not logging this run's updates to Supabase.")

    # Step 4: Supabase logging + archive queueing, only for commits that are on GitHub
    if pushed:
        for rec in pending:
            attachments = rec.pop("attachments")
            log_to_supabase(rec)
            enqueue_archives(rec["site_slug"], rec["commit_hash"], rec["url"], attachments)

    # Step 4b: Retry summaries that failed in earlier runs (needs full history: fetch-depth 0)
    backfill_summaries(args.data_dir)

    # Step 5: Build Dashboard (also commits and pushes public/)
    print("=== Step 5: Build Static Dashboard ===")
    build_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_dashboard.py")
    build = subprocess.run([sys.executable, build_script, "--data-dir", args.data_dir])
    sys.exit(0 if build.returncode == 0 and pushed else 1)


if __name__ == "__main__":
    main()
