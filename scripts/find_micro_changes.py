"""List "micro changes": reported updates whose only real change is a few characters of text, such as a `NEW!!` badge.

    python scripts/find_micro_changes.py --data-dir ../g-i-t-data --days 30
    python scripts/find_micro_changes.py --data-dir ../g-i-t-data --days 90 --max-chars 40 --format markdown --out micro.md

A reader should be told about an announcement, a document or a date, not about a badge that appears and disappears.
Every `Update <slug>` commit of the period is read once with `analyze_diff()` (the same reading that feeds the AI and the
reorder check): the net lines that really changed, their visible text (tags removed, entities decoded, white space
collapsed). An update is a micro change when

  * no net line adds or removes a link or an image address (`href=` / `src=`: a new document is always real), and
  * the visible text of all net lines together is at most --max-chars characters (default 30) in at most 12 lines.

The micro changes are then grouped by their SIGNATURE (NFKC, lower case, every digit written as 0, `+` / `-` kept), so that
`NEW!!` appearing on 40 sites is one row, "40 sites, 63 updates": a pattern that is worth a rule in the built-in list.
`kind` tells what it probably is: `badge` (NEW / 新着 / 更新 / UP / new!), `numbers` (the same text with other digits:
counters, dates, "5 件"), `text` (anything else: read the sample). Per site the share of micro changes in all its updates is
listed too: a site where most updates are micro is the one to fix first (`rebuild_site_history.py` then removes the past
ones). Needs the history of g-i-t-data (a partial clone is enough: every commit loads its blobs).
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UPDATE = re.compile(r"^Update (\S+)$")
TAG = re.compile(r"<[^>]*>")
BADGE_WORDS = {"new", "新着", "新", "更新", "up", "update", "最新", "注目", "重要", "pickup"}
MAX_LINES = 12


def git(repo, args, check=True):
    res = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 and check:
        sys.exit(f"git {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res.stdout


def visible(text: str) -> str:
    """The text a reader would see in one stored line: tags removed, entities decoded, white space collapsed."""
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", text))).strip()


def signature(sign_texts) -> str:
    """NFKC, lower case, digits as 0, sign kept: `+NEW!!`, `-5件|+6件` -> `-0件|+0件`."""
    parts = sorted(f"{s}{re.sub(r'[0-9]', '0', unicodedata.normalize('NFKC', t).lower())}" for s, t in sign_texts)
    return " | ".join(parts)


def classify(sign_texts) -> str:
    plus = Counter(re.sub(r"[0-9]", "0", unicodedata.normalize("NFKC", t).lower()) for s, t in sign_texts if s == "+")
    minus = Counter(re.sub(r"[0-9]", "0", unicodedata.normalize("NFKC", t).lower()) for s, t in sign_texts if s == "-")
    if plus and plus == minus:
        return "numbers"
    words = [re.sub(r"[\s!+/\-0-9\[\]【】()（）]+", "", unicodedata.normalize("NFKC", t)).lower() for _s, t in sign_texts]
    if all(w in BADGE_WORDS for w in words):
        return "badge"
    return "text"


def micro_change(analysis, max_chars: int):
    """[(sign, visible text)] of a micro change, or None when the update is something a reader should see."""
    sign_texts = []
    for _path, lines in analysis.net_files:
        for sign, text in lines:
            if "href=" in text or "src=" in text:
                return None
            v = visible(text)
            if v:
                sign_texts.append((sign, v))
    if not sign_texts or len(sign_texts) > MAX_LINES:
        return None
    if sum(len(t) for _s, t in sign_texts) > max_chars:
        return None
    return sign_texts


def updates(repo: str, days: int, site: str):
    """[(commit, iso date, slug)] oldest first."""
    out = git(repo, ["log", "--reverse", f"--since={days} days ago", "--date=iso-strict", "--format=%H%x09%ad%x09%s", "--", "sites"])
    rows = []
    for line in out.splitlines():
        commit, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        m = UPDATE.match(subject)
        if m and (not site or m.group(1) == site):
            rows.append((commit, date, m.group(1)))
    return rows


def first_snapshot(repo: str, commit: str, slug: str) -> bool:
    """The first fetch of a site adds every file (no parent version): it is never a change to judge."""
    out = git(repo, ["diff-tree", "--no-commit-id", "--name-status", "-r", "--no-renames", commit, "--", f"sites/{slug}"])
    states = [l.split("\t", 1)[0] for l in out.splitlines() if l and not l.endswith("website-stalker.yaml")]
    return bool(states) and all(s == "A" for s in states)


def scan(repo: str, days: int, site: str, max_chars: int):
    import website_stalk as ws
    per_site = defaultdict(lambda: {"updates": 0, "micro": 0})
    groups = defaultdict(lambda: {"kind": "", "commits": [], "sites": set(), "sample": None})
    for commit, date, slug in updates(repo, days, site):
        if first_snapshot(repo, commit, slug):
            continue
        patch = git(repo, ["show", "--format=", "--patch", "-U0", commit, "--", f"sites/{slug}"])
        if not patch.strip():
            continue
        per_site[slug]["updates"] += 1
        micro = micro_change(ws.analyze_diff(patch), max_chars)
        if micro is None:
            continue
        per_site[slug]["micro"] += 1
        g = groups[signature(micro)]
        g["kind"] = classify(micro)
        g["commits"].append((commit[:7], date[:16], slug))
        g["sites"].add(slug)
        g["sample"] = g["sample"] or [f"{s}{t}" for s, t in micro][:4]
    return per_site, groups


def site_names(repo: str) -> dict:
    try:
        import provision
        return {s["slug"]: s["name"] for s in provision.configured_sites(repo)}
    except Exception:
        return {}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="List updates whose only change is a few characters (badges, counters)")
    parser.add_argument("--data-dir", default="../g-i-t-data")
    parser.add_argument("--days", type=int, default=30, help="period (default 30 days)")
    parser.add_argument("--max-chars", type=int, default=30, help="most visible characters of a micro change (default 30)")
    parser.add_argument("--site", default="", help="only this slug")
    parser.add_argument("--min-updates", type=int, default=1, help="list patterns seen in at least this many updates (default 1)")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    parser.add_argument("--out", default="", help="write the report to this file instead of the screen")
    args = parser.parse_args()

    per_site, groups = scan(args.data_dir, args.days, args.site, args.max_chars)
    names = site_names(args.data_dir)
    total = sum(s["updates"] for s in per_site.values())
    micro_total = sum(s["micro"] for s in per_site.values())
    patterns = sorted(({"signature": k, "kind": g["kind"], "updates": len(g["commits"]), "sites": sorted(g["sites"]),
                        "sample": g["sample"], "last": g["commits"][-1]} for k, g in groups.items()
                       if len(g["commits"]) >= args.min_updates),
                      key=lambda p: (-len(p["sites"]), -p["updates"], p["signature"]))
    sites = sorted(({"slug": slug, "name": names.get(slug, ""), "updates": s["updates"], "micro": s["micro"],
                     "share": round(s["micro"] / s["updates"], 2)} for slug, s in per_site.items() if s["micro"]),
                   key=lambda r: (-r["micro"], -r["share"], r["slug"]))

    if args.format == "json":
        text = json.dumps({"updates": total, "micro": micro_total, "patterns": patterns, "sites": sites}, ensure_ascii=False, indent=2)
    else:
        md = args.format == "markdown"
        text = (f"# Micro changes ({args.days} days)\n\n" if md else "")
        text += (f"{micro_total} of {total} updates ({int(100 * micro_total / total) if total else 0}%) change at most "
                 f"{args.max_chars} visible characters and no link.\n")
        text += "\n" + ("## Patterns\n\n" if md else "Patterns (a pattern on many sites is a candidate for the built-in rules):\n")
        for p in patterns:
            sample = " ".join(p["sample"])
            shown = ", ".join(p["sites"][:5]) + (f" … (+{len(p['sites']) - 5})" if len(p["sites"]) > 5 else "")
            text += (f"\n{'- ' if md else ''}[{p['kind']}] {len(p['sites'])} site(s), {p['updates']} update(s): `{sample}`\n"
                     f"    sites: {shown}; last {p['last'][1]} {p['last'][0]} ({p['last'][2]})\n")
        if not patterns:
            text += "\nNothing found.\n"
        text += "\n" + ("## Sites\n\n" if md else "Sites with micro changes (most first; fix these, then run rebuild_site_history.py):\n")
        for r in sites[:40]:
            text += f"\n{'- ' if md else '  '}{r['slug']} {r['name']}: {r['micro']} of {r['updates']} updates ({int(r['share'] * 100)}%)"
        text += "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{len(patterns)} pattern(s), {len(sites)} site(s) written to {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
