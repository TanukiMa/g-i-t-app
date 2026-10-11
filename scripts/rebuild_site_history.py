"""Re-judge the past updates of a site with its CURRENT ignore rules, and remove those that are no news any more.

    python scripts/rebuild_site_history.py --site jsnfs --data-dir ../g-i-t-data             # dry run (the default): only lists
    python scripts/rebuild_site_history.py --site jsnfs --data-dir ../g-i-t-data --apply     # removes the noise updates

When the `editors:` of a site gain a rule (a `NEW!!` badge, a counter ...), the updates reported BEFORE the rule are
still in the history. This script replays them: for every `Update <slug>` commit of the site it takes the page files
before and after that commit, applies the `regex_replace` editors of `sites/<slug>/website-stalker.yaml` (plus the current
built-in rules, which the pipeline adds to that file on its next run; `--no-defaults` leaves them out) to both, and
reads the difference with `analyze_diff()` (the reading used by the pipeline). Verdicts:

  noise    nothing real is left (no net text, or only moved lines): the update would not be reported today
  reduced  something real is left, but less than before (a real addition together with a badge): kept; summarize again
           with `resummarize.py --commit <hash> --force` if the summary mentions the badge
  kept     the same real change as before

Limits, on purpose: only `regex_replace` editors can be replayed. The stored pages are the OUTPUT of the editors
(`css_select`, `css_remove`, `html_sanitize` have already run: class names and ids are gone), so `remove:` selectors
cannot be tested on them; rules the Rust regex engine of website-stalker reads differently from Python are reported as
skipped. The git history is never rewritten.

`--apply` (after reading the dry run) does, for the noise updates only:
  1. writes a backup of the rows to rebuild_backup_<slug>_<time>.json (--backup-dir),
  2. deletes the `updates` rows and the `pending` archive_queue rows of those commits (rows already `done` or `failed`
     in archive_queue are kept: the Wayback copies exist, and nothing links to them any more),
  3. deletes `public/sites/<slug>/diff_<hash7>.html` of those commits in the data checkout.
Then commit and push g-i-t-data (`git add -A public && git commit && git push`) and run `g-i-t-remake-dashboard`: the
history page, the feeds, `search.json` and `status.json` are rebuilt from the remaining rows.
Needs SUPABASE_URL / SUPABASE_KEY for the row ids and `--apply` (a dry run without them still gives the verdicts).
"""
import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_micro_changes import first_snapshot, git, visible  # noqa: E402


def load_rules(data_dir: str, slug: str, with_defaults: bool = True):
    """([(compiled regex, replacement)], [skipped descriptions]) from the regex_replace editors of the site's stalker file.

    With `with_defaults` the CURRENT built-in rules (provision.DEFAULT_IGNORE_RULES) are added to those in the file: the
    pipeline appends them to the file on its next run, so this is the state the site is about to have."""
    import provision
    path = os.path.join(data_dir, "sites", slug, "website-stalker.yaml")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    editors = cfg["sites"][0].get("editors") or []
    if with_defaults:
        editors = provision.merge_editors(editors, provision.rules_to_editors(provision.DEFAULT_IGNORE_RULES, slug))
    rules, skipped = [], []
    for editor in editors:
        if not (isinstance(editor, dict) and "regex_replace" in editor):
            continue
        spec = editor["regex_replace"]
        pattern, replace = spec.get("pattern", ""), spec.get("replace", "")
        try:
            # website-stalker writes $1 / ${1} for a group; Python writes \g<1>
            rules.append((re.compile(pattern), re.sub(r"\$\{?(\d+)\}?", r"\\g<\1>", replace)))
        except re.error as e:
            skipped.append(f"{pattern[:60]} ({e})")
    return rules, skipped


def normalize(text: str, rules) -> str:
    for regex, replace in rules:
        text = regex.sub(replace, text)
    return text


def changed_files(data_dir: str, commit: str, slug: str) -> list:
    out = git(data_dir, ["diff-tree", "--no-commit-id", "--name-status", "-r", "--no-renames", commit, "--", f"sites/{slug}"])
    files = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if status == "M" and not path.endswith("website-stalker.yaml"):
            files.append(path)
    return files


def replay_patch(data_dir: str, commit: str, files: list, rules) -> str:
    """A unified diff (zero context) of the files between commit^ and commit AFTER the current rules were applied."""
    parts = []
    for path in files:
        old = git(data_dir, ["show", f"{commit}^:{path}"], check=False)
        new = git(data_dir, ["show", f"{commit}:{path}"], check=False)
        old_lines = normalize(old, rules).splitlines()
        new_lines = normalize(new, rules).splitlines()
        body = list(difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""))
        if body:
            parts.append(f"diff --git a/{path} b/{path}\n" + "\n".join(body))
    return "\n".join(parts)


def remaining(analysis, limit: int = 3) -> list:
    lines = [f"{s}{(visible(t) or t.strip())[:70]}" for _p, ls in analysis.net_files for s, t in ls]
    return lines[:limit] + ([f"… (+{len(lines) - limit})"] if len(lines) > limit else [])


def judge(data_dir: str, commit: str, slug: str, rules):
    """(verdict, chars before, chars after, remaining lines) of one Update commit."""
    import website_stalk as ws
    files = changed_files(data_dir, commit, slug)
    before = ws.analyze_diff(git(data_dir, ["show", "--format=", "--patch", "-U0", commit, "--", f"sites/{slug}"]))
    before_text = before.text() or ""
    patch = replay_patch(data_dir, commit, files, rules)
    if not patch:
        return "noise", len(before_text), 0, []
    after = ws.analyze_diff(patch)
    after_text = after.text() or ""
    if after.moved_only or (after.has_text_hunks and not after_text.strip()):
        return "noise", len(before_text), 0, []
    verdict = "reduced" if len(after_text) < 0.9 * len(before_text) else "kept"
    return verdict, len(before_text), len(after_text), remaining(after)


def site_updates(data_dir: str, slug: str, since: str):
    args = ["log", "--reverse", "--date=iso-strict", "--format=%H%x09%ad%x09%s", f"--grep=^Update {slug}$", "--", f"sites/{slug}"]
    if since:
        args.insert(1, f"--since={since}")
    rows = []
    for line in git(data_dir, args).splitlines():
        commit, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        if subject == f"Update {slug}":
            rows.append((commit, date))
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Re-judge past updates of a site with its current ignore rules")
    parser.add_argument("--site", required=True, help="slug, or several separated by commas")
    parser.add_argument("--data-dir", default="../g-i-t-data")
    parser.add_argument("--since", default="", help="only commits since this date (default: the whole history)")
    parser.add_argument("--no-defaults", action="store_true",
                        help="replay only the rules written in the site's website-stalker.yaml (default: plus the current built-in rules)")
    parser.add_argument("--apply", action="store_true", help="remove the noise updates (default: only list them)")
    parser.add_argument("--backup-dir", default=".", help="where --apply writes the backup of the removed rows")
    args = parser.parse_args()

    supabase = None
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        import website_stalk as ws
        supabase = ws.get_supabase()
    if args.apply and not supabase:
        sys.exit("--apply needs SUPABASE_URL / SUPABASE_KEY.")

    for slug in [s.strip() for s in args.site.split(",") if s.strip()]:
        rules, skipped = load_rules(args.data_dir, slug, not args.no_defaults)
        print(f"\n== {slug}: {len(rules)} regex rule(s) replayed" + (f", {len(skipped)} skipped: {'; '.join(skipped)}" if skipped else ""))
        noise, counts = [], {"noise": 0, "reduced": 0, "kept": 0, "first": 0}
        for commit, date in site_updates(args.data_dir, slug, args.since):
            if first_snapshot(args.data_dir, commit, slug):
                counts["first"] += 1
                continue
            verdict, before, after, left = judge(args.data_dir, commit, slug, rules)
            counts[verdict] += 1
            row = None
            if supabase:
                data = supabase.table("updates").select("id,summary").eq("site_slug", slug).eq("commit_hash", commit).execute().data
                row = data[0] if data else None
            mark = "" if supabase is None else (" [no row]" if row is None else f" [row {row['id']}]")
            print(f"  {commit[:7]} {date[:16]} {verdict:<7} net chars {before} -> {after}{mark}")
            for l in left:
                print(f"      {l}")
            if verdict == "noise" and row is not None:
                noise.append((commit, row))
        print(f"  summary: noise {counts['noise']}, reduced {counts['reduced']}, kept {counts['kept']}, first snapshots {counts['first']}")

        if not noise:
            continue
        if not args.apply:
            print(f"  (dry run: {len(noise)} row(s) would be removed; run again with --apply)")
            continue
        os.makedirs(args.backup_dir, exist_ok=True)
        backup = {"site": slug, "updates": [], "archive_queue": []}
        for commit, row in noise:
            backup["updates"] += supabase.table("updates").select("*").eq("id", row["id"]).execute().data
            backup["archive_queue"] += supabase.table("archive_queue").select("*").eq("site_slug", slug).eq("commit_hash", commit).execute().data
        path = os.path.join(args.backup_dir, f"rebuild_backup_{slug}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2, default=str)
        print(f"  backup written: {path}")
        for commit, row in noise:
            supabase.table("archive_queue").delete().eq("site_slug", slug).eq("commit_hash", commit).eq("status", "pending").execute()
            supabase.table("updates").delete().eq("id", row["id"]).execute()
            diff_page = os.path.join(args.data_dir, "public", "sites", slug, f"diff_{commit[:7]}.html")
            if os.path.exists(diff_page):
                os.remove(diff_page)
            print(f"  removed {commit[:7]} (row {row['id']})")
        print("  Next: commit and push g-i-t-data (git add -A public; git commit; git push), then run g-i-t-remake-dashboard.")


if __name__ == "__main__":
    main()
