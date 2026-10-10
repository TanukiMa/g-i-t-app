"""Recover the updates that a Re-baseline commit swallowed.

    python scripts/recover_rebaselined.py --data-dir ../g-i-t-data --dry-run
    python scripts/recover_rebaselined.py --data-dir ../g-i-t-data --limit 5 --pages
    python scripts/recover_rebaselined.py --data-dir ../g-i-t-data --exclude jsmez,digitalmeddx --pages

Before website_stalk.added_text_lines() was used, the first run after a change of a site's ignore/remove rules was committed
as `Re-baseline <slug> after ignore rule change` and never reported, even when the page also gained real text in that run
(the update of jami.jp of 2026-10-07 was lost this way). This script finds those commits (a Re-baseline whose diff adds
visible text; a rule only deletes text or shortens an address), makes the AI summary of each, and inserts the row into
`updates` as if it had been reported then: `created_at` is the TIME OF THE COMMIT, so the dashboard shows it where it belongs.

  * Model: --model (default gemini-3.5-flash-lite); the optional fallback provider is never used, so the recorded
    summary_model is the model that was asked for. Needs GEMINI_API_KEY (not for --dry-run). google-genai is not needed:
    without it the REST API is called with `requests`.
  * Database: with the Python package `supabase` (SUPABASE_URL / SUPABASE_KEY) or, when it is not installed, through the
    Supabase CLI that is linked to the project (`supabase db query --linked`, or `npx supabase`): no virtual environment
    needed. --db python|cli|auto (default auto).
  * Rows already in `updates` (same commit) are skipped: the script can be run again after a quota stop. A summary that
    says "no substantive change" is not recorded (the lines that were added were noise).
  * --pages writes the diff page into <data-dir>/public/sites/<slug>/diff_<hash7>.html (diff2html-cli on the PATH, or npx);
    commit and push g-i-t-data afterwards (the next dashboard build decorates it). Without --pages the card has no "差分" link.
  * No archive_queue rows: a copy of the page made now would not show the page as it was at that commit.
Needs the full history of g-i-t-data.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_CONSECUTIVE_FAILURES = 3
FLUSH_EVERY = 10          # rows per call of the CLI (one call takes several seconds)
ROW_COLUMNS = ["site_slug", "domain", "url", "commit_hash", "summary", "summary_model", "created_at"]


def sql_text(value: str) -> str:
    """A text literal made of ASCII only (U&'...' with \\XXXX escapes): no encoding problem on the way to the CLI."""
    out = []
    for ch in value:
        o = ord(ch)
        if ch == "'":
            out.append("''")
        elif ch == "\\":
            out.append("\\\\")
        elif 32 <= o < 127:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append("\\%04x" % o)
        else:
            out.append("\\+%06x" % o)
    return "U&'" + "".join(out) + "'"


class PythonDb:
    def __init__(self, client):
        self.client = client

    def known_hashes(self) -> set:
        known, offset = set(), 0
        while True:
            page = self.client.table("updates").select("commit_hash").range(offset, offset + 999).execute().data or []
            known |= {r["commit_hash"] for r in page}
            if len(page) < 1000:
                return known
            offset += 1000

    def insert(self, rows: list):
        for row in rows:
            try:
                self.client.table("updates").insert(row).execute()
            except Exception:
                self.client.table("updates").insert({k: v for k, v in row.items() if k != "summary_model"}).execute()


class CliDb:
    """The linked Supabase project through the Supabase CLI (`supabase db query --linked -f file.sql`)."""

    def __init__(self):
        exe = shutil.which("supabase.exe") or shutil.which("supabase")
        # the wrapper "supabase" of some shells drops arguments: prefer the real program, else npx
        self.base = [exe] if exe and exe.lower().endswith(".exe") else [shutil.which("npx") or "npx", "--yes", "supabase"]

    def query(self, sql: str) -> list:
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="ascii", newline="\n") as f:
            f.write(sql)
            path = f.name
        try:
            env = dict(os.environ, npm_config_yes="true")      # npx must not stop at "Ok to proceed?"
            # -o json: in a terminal the CLI would print a table; an AI agent's shell gets JSON by itself
            res = subprocess.run(self.base + ["db", "query", "--linked", "-o", "json", "-f", path], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL)
        finally:
            os.unlink(path)
        out = res.stdout
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        if res.returncode != 0 or not starts:
            raise RuntimeError(f"supabase db query failed (exit {res.returncode}); stdout: {out[-300:]!r}; stderr: {res.stderr[-300:]!r}")
        data = json.JSONDecoder().raw_decode(out[min(starts):])[0]     # the first JSON value; nothing after it matters
        return data.get("rows", []) if isinstance(data, dict) else data

    def known_hashes(self) -> set:
        return {r["commit_hash"] for r in self.query("select commit_hash from updates;")}

    def insert(self, rows: list):
        if not rows:
            return
        values = ",\n".join("(" + ", ".join([sql_text(r[c]) for c in ROW_COLUMNS[:-1]] + [sql_text(r["created_at"]) + "::timestamptz"]) + ")"
                            for r in rows)
        self.query(f"insert into updates ({', '.join(ROW_COLUMNS)})\n"
                   f"select * from (values\n{values}\n) as v({', '.join(ROW_COLUMNS)})\n"
                   "where not exists (select 1 from updates u where u.commit_hash = v.commit_hash);")


def open_db(kind: str, ws):
    if kind in ("auto", "python"):
        client = ws.get_supabase()
        if client:
            return PythonDb(client)
        if kind == "python":
            sys.exit("--db python needs the package supabase and SUPABASE_URL / SUPABASE_KEY.")
    print("Using the Supabase CLI (supabase db query --linked) for the database.")
    return CliDb()


def parse_args():
    p = argparse.ArgumentParser(description="Recover the updates swallowed by Re-baseline commits")
    p.add_argument("--data-dir", default="../g-i-t-data", help="g-i-t-data checkout with its full history")
    p.add_argument("--model", default="gemini-3.5-flash-lite", help="model for the summaries (default gemini-3.5-flash-lite)")
    p.add_argument("--site", default="", help="comma separated slugs")
    p.add_argument("--exclude", default="", help="comma separated slugs to leave out")
    p.add_argument("--since", default="", help="only commits since this date (e.g. 2026-10-01)")
    p.add_argument("--limit", type=int, default=0, help="at most this many rows per run (0 = all)")
    p.add_argument("--pages", action="store_true", help="also write the diff pages")
    p.add_argument("--db", choices=("auto", "python", "cli"), default="auto", help="how to reach Supabase (default auto)")
    p.add_argument("--max-minutes", type=float, default=30, help="stop starting new summaries after this (default 30)")
    p.add_argument("--dry-run", action="store_true", help="list what would be recovered, change nothing")
    return p.parse_args()


def rebaseline_commits(repo: str, since: str, sites: set) -> list:
    """[(full hash, ISO time of the commit in UTC, slug)] oldest first."""
    cmd = ["git", "log", "--reverse", "--grep=^Re-baseline", "--format=%H\t%cI\t%s"]
    if since:
        cmd.append(f"--since={since}")
    out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    rows = []
    for line in out.splitlines():
        h, when, subject = line.split("\t", 2)
        parts = subject.split()
        slug = parts[1] if len(parts) > 1 else ""
        if slug and (not sites or slug in sites):
            when_utc = datetime.fromisoformat(when).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append((h, when_utc, slug))
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = parse_args()
    os.environ["STALK_BUDGET_MIN"] = str(args.max_minutes + 8)   # website_stalk reads its budget at import
    import website_stalk as ws
    import provision

    db = open_db(args.db, ws)
    urls = {s["slug"]: s["url"] for s in provision.configured_sites(args.data_dir)}
    sites = {s.strip() for s in args.site.split(",") if s.strip()}
    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}

    try:
        known = db.known_hashes()
    except Exception as e:
        sys.exit(f"Could not read updates: {e}")

    todo = []
    for h, when, slug in rebaseline_commits(args.data_dir, args.since, sites):
        if h in known or slug in excluded:
            continue
        patch = ws.git(["show", "--format=", "--patch", h, "--", f"sites/{slug}"], args.data_dir).stdout
        added = ws.analyze_diff(patch).added_text()
        if added:
            todo.append({"hash": h, "when": when, "slug": slug, "patch": patch, "added": added})
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} swallowed update(s) to recover (model {args.model}, created_at = time of the commit).")
    for t in todo:
        print(f"  {t['when']}  {t['slug']:<28} {t['hash'][:7]}  +{len(t['added'])}  {t['added'][0][:50]}")
    if args.dry_run or not todo:
        print("Dry run: nothing was changed." if args.dry_run else "Nothing to do.")
        return

    import time
    deadline = time.monotonic() + args.max_minutes * 60
    done = failures = skipped = 0
    pending = []

    def flush():
        if pending:
            db.insert(pending)
            print(f"  ... {len(pending)} row(s) stored.")
            pending.clear()

    try:
        for t in todo:
            if time.monotonic() >= deadline:
                print("Time budget used up; run again to continue.")
                break
            summary, model = ws.summarize_diff_with_gemini(t["patch"], models=[args.model], allow_fallback=False)
            if model is None:           # quota, timeout, no key ...: nothing is stored, the next run does it
                failures += 1
                print(f"  {t['slug']} {t['hash'][:7]}: no summary ({summary[:30]}); not stored.")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"{MAX_CONSECUTIVE_FAILURES} failures in a row (quota?): stopping; run again later.")
                    break
                continue
            failures = 0
            if summary.startswith("内容に実質的な変更はありません"):
                skipped += 1            # the lines that were added turned out to be noise (a label, a skip link ...)
                print(f"  {t['slug']} {t['hash'][:7]}: the model sees no substantive change; not recorded.")
                continue
            target = urls.get(t["slug"], "")
            pending.append({"site_slug": t["slug"], "domain": urlparse(target).netloc if target else t["slug"],
                            "url": target or f"https://{t['slug']}", "commit_hash": t["hash"], "summary": summary,
                            "summary_model": model, "created_at": t["when"]})
            if args.pages:
                html = ws.render_diff_html(t["hash"], args.data_dir, f"sites/{t['slug']}")
                if html:
                    folder = os.path.join(args.data_dir, "public", "sites", t["slug"])
                    os.makedirs(folder, exist_ok=True)
                    with open(os.path.join(folder, f"diff_{t['hash'][:7]}.html"), "w", encoding="utf-8") as f:
                        f.write(html)
            done += 1
            print(f"  {t['when']}  {t['slug']:<28} {t['hash'][:7]}  [{model}]  {summary.splitlines()[0][:60] if summary else ''}")
            if len(pending) >= FLUSH_EVERY:
                flush()
    finally:
        flush()          # whatever was summarized is stored, also after an error or a quota stop
    print(f"recovered {done}, {skipped} judged to be noise; {len(todo) - done - skipped} left (run again for the rest)."
          + ("  Commit and push g-i-t-data to publish the diff pages." if args.pages and done else ""))


if __name__ == "__main__":
    main()
