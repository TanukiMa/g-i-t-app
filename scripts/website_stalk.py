import os
import sys
import shutil
import subprocess
import argparse
from urllib.parse import urljoin, urlparse
import yaml
from build_dashboard import commit_and_push_parent
from bs4 import BeautifulSoup

try:
    from google import genai
except ImportError:
    genai = None

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

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
        print(f"Command failed code {res.returncode}: {res.stderr}")
    return res


def summarize_diff_with_gemini(diff_text: str) -> str:
    if not diff_text.strip():
        return "更新差分はありませんでした。"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not genai:
        print("GEMINI_API_KEY is not set or google-generativeai module is missing. Skipping AI summarization.")
        return "Gemini APIキー未設定のため自動要約はスキップされました。"

    try:
        client = genai.Client(api_key=api_key)
        prompt = (
            "以下のテキストはWebサイトのHTML更新差分(Git Diff)です。\n"
            "非エンジニア向けに、何が変更されたかを自然な日本語で箇条書き要約してください。\n"
            "システムコードやタグは無視し、意味のあるコンテンツの変更のみ抽出してください。\n\n"
            f"```diff\n{diff_text[:10000]}\n```"
        )
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except Exception as e:
        # Details go to the log only; the summary is published on the dashboard.
        print(f"Error during Gemini API call: {e}")
        return "AI要約を生成できませんでした。"


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


def checkout_main(site_path: str):
    """Submodules are checked out detached; move onto an up-to-date local main."""
    fetch = run_cmd(["git", "fetch", "origin", "main"], cwd=site_path)
    if fetch.returncode == 0:
        run_cmd(["git", "checkout", "-B", "main", "origin/main"], cwd=site_path)
    else:
        run_cmd(["git", "checkout", "-B", "main"], cwd=site_path)


def render_diff_html(commit_hash: str, site_path: str):
    """Return side-by-side diff HTML for a commit, or None on failure."""
    patch = run_cmd(["git", "show", "--format=", "--patch", commit_hash], cwd=site_path)
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


def process_site_submodule(data_dir: str, site_slug: str):
    site_path = os.path.join(data_dir, "sites", site_slug)
    config_file = os.path.join(site_path, "website-stalker.yaml")

    if not os.path.exists(config_file):
        print(f"Config {config_file} does not exist. Skipping.")
        return

    # Extract target URL from config
    target_url = ""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            if isinstance(cfg, dict) and "sites" in cfg and len(cfg["sites"]) > 0:
                target_url = cfg["sites"][0].get("url", "")
            elif isinstance(cfg, list) and len(cfg) > 0:
                target_url = cfg[0].get("url", "")
    except Exception as e:
        print(f"Error reading {config_file}: {e}")

    domain = urlparse(target_url).netloc if target_url else site_slug

    checkout_main(site_path)

    # 1. Execute website-stalker
    print(f"--- Running website-stalker in {site_path} ---")
    run_cmd(["website-stalker", "run", "--all"], cwd=site_path)

    # 2. Check git status
    status_res = run_cmd(["git", "status", "--porcelain"], cwd=site_path)
    if not status_res.stdout.strip():
        print(f"No changes detected in {site_slug}.")
        return

    run_cmd(["git", "add", "-A"], cwd=site_path)
    raw_diff = run_cmd(["git", "diff", "--cached"], cwd=site_path).stdout

    # 3. AI Summarization
    summary = summarize_diff_with_gemini(raw_diff)

    # Commit the content change. This hash is the one logged and never amended.
    commit_res = run_cmd(["git", "commit", "-m", f"Automated stalker update for {site_slug}"], cwd=site_path)
    if commit_res.returncode != 0:
        print(f"Commit failed for {site_slug}; skipping.")
        return
    commit_hash = run_cmd(["git", "rev-parse", "HEAD"], cwd=site_path).stdout.strip()

    # 4. Diff HTML, committed separately so commit_hash stays stable
    diff_html = render_diff_html(commit_hash, site_path)
    if diff_html:
        diff_html_filename = f"diff_{commit_hash[:7]}.html"
        with open(os.path.join(site_path, diff_html_filename), "w", encoding="utf-8") as f:
            f.write(diff_html)
        run_cmd(["git", "add", diff_html_filename], cwd=site_path)
        run_cmd(["git", "commit", "-m", f"Add diff view for {commit_hash[:7]}"], cwd=site_path)
    else:
        print(f"Failed to generate diff HTML for {site_slug}.")

    # Push before logging so the DB never references an unpublished commit
    push_res = run_cmd(["git", "push", "origin", "main"], cwd=site_path)
    if push_res.returncode != 0:
        print(f"Push failed for {site_slug}; not logging this update.")
        return

    # 5. Supabase Logging
    page_url = target_url or f"https://{domain}"
    log_to_supabase({
        "site_slug": site_slug,
        "domain": domain,
        "url": page_url,
        "commit_hash": commit_hash,
        "summary": summary,
    })

    # 6. Queue Wayback archiving (page + newly linked documents); handled by archive_worker.py
    enqueue_archives(site_slug, commit_hash, page_url, extract_attachment_links(raw_diff, page_url))


def main():
    parser = argparse.ArgumentParser(description="G-I-T Processing Pipeline")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    if not shutil.which("website-stalker"):
        print("website-stalker is not installed or not on PATH.")
        sys.exit(1)

    # Step 1: Run Provisioning
    print("=== Step 1: JIT Auto-Provisioning ===")
    provision_script = os.path.join(os.path.dirname(__file__), "provision.py")
    prov = subprocess.run([sys.executable, provision_script, "--data-dir", args.data_dir])
    if prov.returncode != 0:
        print(f"Provisioning exited with {prov.returncode}")
    # Persist new submodules (.gitmodules + pointers) right away, independent of later steps.
    if not commit_and_push_parent(args.data_dir):
        print("Could not push parent repository after provisioning.")

    # Step 2: Iterate over all submodules in sites/
    print("=== Step 2: Stalk & Process Submodules ===")
    sites_dir = os.path.join(args.data_dir, "sites")
    if os.path.exists(sites_dir):
        for entry in sorted(os.listdir(sites_dir)):
            site_path = os.path.join(sites_dir, entry)
            if os.path.isdir(site_path):
                try:
                    process_site_submodule(args.data_dir, entry)
                except Exception as e:
                    print(f"Error processing submodule {entry}: {e}")

    # Step 3: Build Dashboard, then commit/push the parent repo
    print("=== Step 3: Build Static Dashboard ===")
    build_script = os.path.join(os.path.dirname(__file__), "build_dashboard.py")
    build = subprocess.run([sys.executable, build_script, "--data-dir", args.data_dir])
    sys.exit(build.returncode)


if __name__ == "__main__":
    main()
