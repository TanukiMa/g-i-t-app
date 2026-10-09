"""List sites whose stored page "flips": a change that is undone by a later change (A -> B, then B -> A).

    python scripts/find_flips.py --data-dir ../g-i-t-data --days 30
    python scripts/find_flips.py --data-dir ../g-i-t-data --days 14 --min-returns 1 --format markdown --out flips.md

A page that goes back to a state it already had is usually not news: the server hands out one of several versions at
random (a category label, a rotating banner, an A/B block, a list in changing order ...). The report shows, per site,
how many of its changes in the period went back to an earlier state ("returns"), the share of all changes, and the
real lines of the latest return, so that the cause can be written as `remove:` / `ignore:` in config.yaml.

How: the page files of every `Update <slug>` / `Reorder <slug>` commit are compared by their git blob ids
(`git log --raw`), nothing is downloaded. A commit "returns" when one of its files becomes a blob that the same file
already had earlier (before the period too). Re-baseline, Provision and rule commits are replayed to keep the state
right, but never counted. A legitimate item that is removed and put back also counts: read the sample lines.
Needs the history of g-i-t-data (a partial clone is enough; the sample lines load a few blobs).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COUNTED = re.compile(r"^(Update|Reorder) (\S+)")
RAW = re.compile(r"^:\d+ \d+ ([0-9a-f]{40}) ([0-9a-f]{40}) \w+\t(.+)$")


def git(repo, args):
    res = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        sys.exit(f"git {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res.stdout


def history(repo: str, days: int):
    """[(commit, iso date, subject, [(path, old_blob, new_blob)])] oldest first, for the period."""
    out = git(repo, ["log", "--reverse", f"--since={days} days ago", "--no-renames", "--raw", "--no-abbrev",
                     "--date=iso-strict", "--format=@@%H%x09%ad%x09%s", "--", "sites"])
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            commit, date, subject = (line[2:].split("\t", 2) + ["", ""])[:3]
            cur = (commit, date, subject, [])
            commits.append(cur)
            continue
        m = RAW.match(line)
        if m and cur is not None and not m.group(3).endswith("website-stalker.yaml"):
            cur[3].append((m.group(3), m.group(1), m.group(2)))
    return commits


def site_of(path: str) -> str:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "sites" else ""


def find_flips(commits):
    """{slug: {"updates": n, "reorders": n, "returns": [(commit, date, path, kind)]}}

    kind "Update" = the change was reported to readers, "Reorder" = it was committed silently (same lines, other order)."""
    seen = defaultdict(set)      # path -> blobs the file has had
    stats = defaultdict(lambda: {"updates": 0, "reorders": 0, "returns": []})
    null = "0" * 40
    for commit, date, subject, files in commits:
        m = COUNTED.match(subject)
        counted_slug = m.group(2) if m else None
        if counted_slug:
            stats[counted_slug]["updates" if m.group(1) == "Update" else "reorders"] += 1
        returned = None
        for path, old, new in files:
            if old != null:
                seen[path].add(old)          # the state before the period is known too
            if new != null and new in seen[path] and counted_slug and site_of(path) == counted_slug:
                returned = returned or path
            if new != null:
                seen[path].add(new)
        if returned:
            stats[counted_slug]["returns"].append((commit, date, returned, m.group(1)))
    return stats


def sample_lines(repo: str, slug: str, commit: str, limit: int = 6) -> list:
    """The real added/removed lines of the latest returning commit (net_diff: moved lines are left out)."""
    import website_stalk as ws
    patch = git(repo, ["show", "--format=", "--patch", commit, "--", f"sites/{slug}"])
    net = ws.net_diff(patch)
    lines = [l for l in (net or "").splitlines() if l and not l.startswith("===")]
    if not lines:   # nothing but moved lines (a swap of two blocks): show a few of them
        moved = [l[0] + l[1:].strip() for l in patch.splitlines()
                 if l[:1] in "+-" and not l.startswith(("+++", "---")) and l[1:].strip()]
        return ["(only lines that moved)"] + [m[:100] for m in moved[:4]] + ([f"… (+{len(moved) - 4} more)"] if len(moved) > 4 else [])
    return [l[:100] for l in lines[:limit]] + ([f"… (+{len(lines) - limit} more)"] if len(lines) > limit else [])


def site_names(repo: str) -> dict:
    try:
        import provision
        return {s["slug"]: s["name"] for s in provision.configured_sites(repo)}
    except Exception:
        return {}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="List sites whose pages flip back to an earlier state")
    parser.add_argument("--data-dir", default="../g-i-t-data")
    parser.add_argument("--days", type=int, default=30, help="period (default 30 days)")
    parser.add_argument("--min-returns", type=int, default=2, help="list sites with at least this many returns (default 2)")
    parser.add_argument("--site", default="", help="only this slug")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    parser.add_argument("--out", default="", help="write the report to this file instead of the screen")
    args = parser.parse_args()

    stats = find_flips(history(args.data_dir, args.days))
    names = site_names(args.data_dir)
    rows = []
    for slug, st in stats.items():
        if args.site and slug != args.site:
            continue
        n = len(st["returns"])
        if n < args.min_returns or n == 0:
            continue
        last = st["returns"][-1]
        reported = sum(1 for r in st["returns"] if r[3] == "Update")
        changes = st["updates"] + st["reorders"]
        rows.append({"slug": slug, "name": names.get(slug, ""), "updates": st["updates"], "reorders": st["reorders"],
                     "returns": n, "reported_returns": reported, "silent_returns": n - reported,
                     "share": round(n / changes, 2) if changes else 0,
                     "last_return": last[1], "last_commit": last[0][:7],
                     "sample": sample_lines(args.data_dir, slug, last[0])})
    rows.sort(key=lambda r: (-r["reported_returns"], -r["returns"], r["slug"]))   # what readers see comes first

    if args.format == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=2)
    elif args.format == "markdown":
        text = f"# Pages that flip back ({args.days} days, at least {args.min_returns} return(s))\n\n"
        text += ("| site | reported updates | silent reorders | returns (reported / silent) | share of all changes | last return | sample of the last return |\n"
                 "|---|---|---|---|---|---|---|\n")
        for r in rows:
            sample = "<br>".join(l.replace("|", "\\|") for l in r["sample"])
            text += (f"| {r['slug']} {r['name']} | {r['updates']} | {r['reorders']} | {r['returns']} ({r['reported_returns']} / {r['silent_returns']}) | "
                     f"{int(r['share'] * 100)}% | {r['last_return'][:16]} `{r['last_commit']}` | {sample} |\n")
    else:
        text = f"Pages that flip back: {len(rows)} site(s) with at least {args.min_returns} return(s) in {args.days} days.\n"
        text += "(returns = changes that restored a state the page already had; a removed-and-restored item counts too.\n"
        text += " reported = shown to readers as an update, silent = a reorder committed without being reported)\n"
        for r in rows:
            text += (f"\n{r['slug']}  {r['name']}\n  reported updates {r['updates']}, silent reorders {r['reorders']}; "
                     f"returns {r['returns']} (reported {r['reported_returns']}, silent {r['silent_returns']}), "
                     f"{int(r['share'] * 100)}% of all changes; last {r['last_return'][:16]} {r['last_commit']}\n")
            text += "".join(f"    {l}\n" for l in r["sample"])
        if not rows:
            text += "\nNothing found.\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{len(rows)} site(s) written to {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
