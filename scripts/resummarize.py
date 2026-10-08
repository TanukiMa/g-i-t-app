"""Regenerate stored AI summaries from the commits in g-i-t-data, with net_diff() as the model's input.

    python scripts/resummarize.py --data-dir ../g-i-t-data --dry-run
    python scripts/resummarize.py --data-dir ../g-i-t-data --limit 50
    python scripts/resummarize.py --data-dir ../g-i-t-data --model gemini-3.8-flash --site mhlw --force

Which rows: by default only rows whose `summary_model` is empty (written before the model was recorded), newest first,
at most --limit per run (default 50, env RESUMMARIZE_LIMIT, 0 = no limit). The first snapshots ("記録を開始しました") are
never touched. --force takes rows that already have a model too; naming commits with --commit implies --force.

Which model: GEMINI_MODELS (default list) unless --model "a,b" names the models to try, in this order. With --model
the optional LLM_FALLBACK_* provider is NOT used, so the recorded model is the one that was asked for.

A row is overwritten only by a real result (an AI summary, or the rule-based "no substantive change"). A failed
attempt (quota, timeout ...) keeps the old summary; three failures in a row stop the run, so a day's quota is not burnt.
Needs the full history of g-i-t-data (the commits are read with `git show`), SUPABASE_URL / SUPABASE_KEY and, unless
--dry-run, GEMINI_API_KEY. The column must exist: sql/add-summary-model.sql.
"""
import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_LIMIT = 50
PAGE = 200
MAX_CONSECUTIVE_FAILURES = 3


def parse_args():
    parser = argparse.ArgumentParser(description="Regenerate AI summaries with net_diff()")
    parser.add_argument("--data-dir", default="../g-i-t-data", help="g-i-t-data checkout with its full history")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("RESUMMARIZE_LIMIT") or DEFAULT_LIMIT),
                        help=f"rows per run (default {DEFAULT_LIMIT}; 0 = no limit)")
    parser.add_argument("--model", default="", help='comma separated model names to use instead of GEMINI_MODELS (no fallback provider)')
    parser.add_argument("--force", action="store_true", help="also rows that already have a summary_model (implied by --commit)")
    parser.add_argument("--site", default="", help="comma separated slugs")
    parser.add_argument("--commit", default="", help="comma separated commit hashes (7 or more characters)")
    parser.add_argument("--since", default="", help="created_at >= (e.g. 2026-10-01)")
    parser.add_argument("--until", default="", help="created_at < (e.g. 2026-10-09)")
    parser.add_argument("--oldest-first", action="store_true", help="default is newest first")
    parser.add_argument("--workers", type=int, default=2, help="summaries made at the same time (default 2)")
    parser.add_argument("--max-minutes", type=float, default=30, help="stop starting new summaries after this (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be done (and how many rows need no AI), change nothing")
    return parser.parse_args()


def csv_list(value: str) -> list:
    return [v.strip() for v in value.split(",") if v.strip()]


def fetch_rows(supabase, args, skip_texts) -> list:
    """Eligible rows, in the requested order, at most args.limit (0 = all)."""
    rows, offset = [], 0
    commits = csv_list(args.commit)
    sites = csv_list(args.site)
    while True:
        q = supabase.table("updates").select("id,site_slug,commit_hash,summary,summary_model")
        if not args.force:
            q = q.is_("summary_model", "null")
        if sites:
            q = q.in_("site_slug", sites)
        if commits:
            q = q.or_(",".join(f"commit_hash.like.{c}*" for c in commits))
        if args.since:
            q = q.gte("created_at", args.since)
        if args.until:
            q = q.lt("created_at", args.until)
        page = (q.order("id", desc=not args.oldest_first).range(offset, offset + PAGE - 1).execute().data or [])
        rows += [r for r in page if r.get("summary") not in skip_texts]
        if len(page) < PAGE or (args.limit and len(rows) >= args.limit):
            break
        offset += PAGE
    return rows[:args.limit] if args.limit else rows


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = parse_args()
    if args.commit:
        args.force = True   # naming a commit is an explicit request: rewrite it even if it already has a model
    # website_stalk reads its run budget at import: give it this run's own (its last 8 minutes are a reserve there).
    os.environ["STALK_BUDGET_MIN"] = str(args.max_minutes + 8)
    import website_stalk as ws
    from common import LEGACY_SUMMARY_INITIAL, SUMMARY_INITIAL

    supabase = ws.get_supabase()
    if not supabase:
        sys.exit("SUPABASE_URL / SUPABASE_KEY are not set.")
    try:
        rows = fetch_rows(supabase, args, {SUMMARY_INITIAL, *LEGACY_SUMMARY_INITIAL})
    except Exception as e:
        sys.exit(f"Could not read updates ({e}). Has sql/add-summary-model.sql been run?")
    models = csv_list(args.model) or None
    print(f"{len(rows)} row(s) selected (limit {args.limit or 'none'}, "
          f"{'all rows' if args.force else 'rows without summary_model'}, "
          f"models: {', '.join(models) if models else ', '.join(ws.GEMINI_MODELS) + ' (+ fallback provider if configured)'}).")

    def patch_of(row):
        res = ws.git(["show", "--format=", "--patch", row["commit_hash"], "--", f"sites/{row['site_slug']}"], args.data_dir)
        return res.stdout if res.returncode == 0 and res.stdout.strip() else ""

    if args.dry_run:
        by_rule = by_ai = missing = 0
        for r in rows:
            patch = patch_of(r)
            net = ws.net_diff(patch) if patch else None
            if not patch:
                missing += 1
            elif net == "":
                by_rule += 1
            else:
                by_ai += 1
            print(f"  {r['id']:>6} {r['site_slug']:<28} {r['commit_hash'][:7]}  "
                  f"{'(commit not found)' if not patch else 'no AI needed' if net == '' else f'{len(patch)} -> {len(net) if net is not None else len(patch)} chars'}")
        print(f"dry run: {by_ai} need the AI, {by_rule} are decided by rule, {missing} commit(s) not found. Nothing changed.")
        return

    stop = threading.Event()
    lock = threading.Lock()
    state = {"fail_streak": 0, "done": 0, "failed": 0}
    deadline = time.monotonic() + args.max_minutes * 60

    def work(row):
        if stop.is_set() or time.monotonic() >= deadline:
            return row, None
        patch = patch_of(row)
        if not patch:
            print(f"  {row['id']} {row['site_slug']} {row['commit_hash'][:7]}: commit not found, kept.")
            return row, None
        try:
            return row, ws.summarize_diff_with_gemini(patch, models=models, allow_fallback=models is None)
        except Exception as e:
            print(f"  {row['id']} {row['site_slug']}: {e}")
            return row, (ws.SUMMARY_FAILED, None)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for fut in as_completed([pool.submit(work, r) for r in rows]):
            row, result = fut.result()
            if result is None:
                continue
            summary, model = result
            with lock:
                if model is None:                      # failed / no key / deferred: keep what is stored
                    state["failed"] += 1
                    state["fail_streak"] += 1
                    if state["fail_streak"] >= MAX_CONSECUTIVE_FAILURES and not stop.is_set():
                        print(f"{MAX_CONSECUTIVE_FAILURES} failures in a row (quota?): stopping; the stored summaries are kept.")
                        stop.set()
                    continue
                state["fail_streak"] = 0
            ws.write_summary(supabase, row["id"], summary, model)
            with lock:
                state["done"] += 1
            print(f"  {row['id']:>6} {row['site_slug']:<28} {row['commit_hash'][:7]}  [{model}]  "
                  f"{summary.splitlines()[0][:70] if summary else ''}")
    print(f"done: {state['done']} rewritten, {state['failed']} failed (kept as they were), "
          f"{len(rows) - state['done'] - state['failed']} not attempted.")
    sys.exit(0 if state["done"] or not rows else 1)


if __name__ == "__main__":
    main()
