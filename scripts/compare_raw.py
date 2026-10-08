"""Compare the raw arm (g-i-t-data-raw, no editors) with the production data (g-i-t-data, editors on).

    python scripts/compare_raw.py --data-dir ../g-i-t-data --raw-dir ../g-i-t-data-raw --since 2026-11-01 --until 2026-12-01

Per site, within the period: how many commits each side made (= how many changes a reader would have seen), how many bytes
those commits changed, and how big the stored pages are now. Written to --out: sites.csv, summary.json and, when matplotlib
is installed (analysis only, not in requirements.txt), figures as SVG and PDF.

Counting rules (g-i-t-data): `Update <slug>` = reported update; `Reorder <slug> ...` and `Re-baseline <slug> ...` are
counted separately (never reported to readers); other commits (Provision, Snapshot, ignore-rule updates) are ignored.
g-i-t-data-raw: `Raw <slug>`; `Baseline <slug>` (first fetch) is ignored. Choose --since after both sides' first fetch and
after the ignore/remove rules settled, otherwise first snapshots and re-baselines distort the comparison.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

SUBJECT = re.compile(r"^(Update|Raw|Reorder|Re-baseline) (\S+)")
KINDS = {"Update": "edited_updates", "Raw": "raw_commits", "Reorder": "edited_reorder", "Re-baseline": "edited_rebaseline"}
MARK = b"@@C\t"

FIELDS = ["slug", "raw_commits", "edited_updates", "edited_reorder", "edited_rebaseline",
          "raw_diff_bytes", "edited_diff_bytes", "raw_head_bytes", "edited_head_bytes"]


def collect(repo: str, since: str, until: str, stats: dict, side: str):
    """Walk `git log -p -U0` once: commits per kind and bytes of added + removed lines, per slug."""
    args = ["git", "-c", "core.quotepath=false", "log", "-p", "-U0", "--no-color", "--no-renames",
            "--format=@@C%x09%H%x09%s"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args += ["--", "sites"]
    proc = subprocess.Popen(args, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    slug = None
    byte_key = "raw_diff_bytes" if side == "raw" else "edited_diff_bytes"
    for raw in proc.stdout:
        line = raw.rstrip(b"\r\n")
        if line.startswith(MARK):
            parts = line.decode("utf-8", "replace").split("\t", 2)
            m = SUBJECT.match(parts[2]) if len(parts) == 3 else None
            slug = None
            if m:
                kind = KINDS[m.group(1)]
                if (side == "raw") == (kind == "raw_commits"):
                    slug = m.group(2)
                    stats[slug][kind] += 1
            continue
        if slug is None or not line or line[:1] not in (b"+", b"-") or line.startswith((b"+++", b"---")):
            continue
        stats[slug][byte_key] += len(line) - 1
    proc.wait()
    if proc.returncode != 0:
        sys.exit(f"git log failed in {repo}: {proc.stderr.read().decode('utf-8', 'replace')}")


def head_bytes(repo: str) -> dict:
    """Bytes of the stored pages (not website-stalker.yaml) per slug at HEAD."""
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-tree", "-r", "-l", "HEAD", "--", "sites"], cwd=repo,
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    sizes = defaultdict(int)
    for row in out.splitlines():
        meta, _, path = row.partition("\t")
        parts = path.split("/")
        if len(parts) < 3 or parts[-1] == "website-stalker.yaml":
            continue
        try:
            sizes[parts[1]] += int(meta.split()[-1])
        except ValueError:
            pass
    return sizes


def pack_kib(repo: str) -> int:
    out = subprocess.run(["git", "count-objects", "-v"], cwd=repo, capture_output=True, text=True).stdout
    vals = dict(l.split(": ") for l in out.splitlines() if ": " in l)
    return int(vals.get("size", 0)) + int(vals.get("size-pack", 0))


def figures(rows, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed: figures skipped (pip install matplotlib).")
        return
    plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "Noto Sans CJK JP", "sans-serif"]

    def save(fig, name):
        for ext in ("svg", "pdf"):
            fig.savefig(os.path.join(out, f"{name}.{ext}"), bbox_inches="tight")
        plt.close(fig)

    def total(key):
        return sum(r[key] for r in rows)

    # 1. commits per site: every dot is a site; below the diagonal the editors removed changes
    fig, ax = plt.subplots(figsize=(5, 5))
    x = [r["raw_commits"] + 1 for r in rows]
    y = [r["edited_updates"] + 1 for r in rows]
    ax.scatter(x, y, s=14, alpha=0.6)
    top = max(x + y + [2])
    ax.plot([1, top], [1, top], "k--", lw=0.8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Changes seen without editors (commits + 1)")
    ax.set_ylabel("Updates reported with editors (+ 1)")
    save(fig, "commits_per_site")

    # 2. totals (log scale): commits and changed bytes
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.6))
    panels = [("Commits", "raw_commits", "edited_updates"), ("Changed bytes", "raw_diff_bytes", "edited_diff_bytes")]
    for ax, (title, raw_key, edited_key) in zip(axes, panels):
        vals = [max(total(raw_key), 1), max(total(edited_key), 1)]
        ax.bar(["no editors", "editors"], vals, color=["#9aa0a6", "#dc2626"])
        ax.set_yscale("log")
        ax.set_title(title)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:,}", ha="center", va="bottom", fontsize=8)
    save(fig, "totals")

    # 3. per-site share of changes that remain with editors
    shares = [r["edited_updates"] / r["raw_commits"] for r in rows if r["raw_commits"] >= 3]
    if shares:
        fig, ax = plt.subplots(figsize=(3.6, 4))
        ax.boxplot(shares, showfliers=True)
        ax.set_ylabel("updates with editors / changes without")
        ax.set_xticks([])
        ax.set_title(f"sites with >= 3 raw changes (n={len(shares)})")
        save(fig, "remaining_share")


def main():
    parser = argparse.ArgumentParser(description="Compare g-i-t-data-raw (no editors) with g-i-t-data (editors)")
    parser.add_argument("--data-dir", default="../g-i-t-data")
    parser.add_argument("--raw-dir", default="../g-i-t-data-raw")
    parser.add_argument("--since", default="", help="e.g. 2026-11-01 (inclusive)")
    parser.add_argument("--until", default="", help="e.g. 2026-12-01")
    parser.add_argument("--out", default="./compare_out")
    args = parser.parse_args()

    stats = defaultdict(lambda: defaultdict(int))
    collect(args.raw_dir, args.since, args.until, stats, "raw")
    collect(args.data_dir, args.since, args.until, stats, "edited")
    raw_head, edited_head = head_bytes(args.raw_dir), head_bytes(args.data_dir)

    rows = []
    for slug in sorted(set(stats) | set(raw_head)):
        row = {"slug": slug, **{k: stats[slug].get(k, 0) for k in FIELDS[1:7]},
               "raw_head_bytes": raw_head.get(slug, 0), "edited_head_bytes": edited_head.get(slug, 0)}
        rows.append(row)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sites.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = {"since": args.since, "until": args.until, "sites": len(rows),
               **{k: sum(r[k] for r in rows) for k in FIELDS[1:]},
               "raw_repo_kib": pack_kib(args.raw_dir), "data_repo_kib": pack_kib(args.data_dir),
               "sites_with_raw_changes": sum(1 for r in rows if r["raw_commits"]),
               "sites_with_reported_updates": sum(1 for r in rows if r["edited_updates"]),
               "note": "repo sizes are loose + packed KiB right now (run git gc first); the data repo also holds history before --since"}
    if summary["raw_commits"]:
        summary["updates_per_raw_change"] = round(summary["edited_updates"] / summary["raw_commits"], 4)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    figures(rows, args.out)


if __name__ == "__main__":
    main()
