"""Writes the patch files (public/sites/<slug>/diff_<hash7>.patch.txt) that the new diff pages are made from.

A diff page used to be diff2html's own HTML (100 KB on average, up to 9 MB), which filled the memory of the Cloud Run
job. The record is now the raw patch of the commit (plain text, 13 KB on average, 24 MB for all 1,800 updates); the page
is a small view that build_dashboard.py makes from it. This script makes the patches for what exists already:

  python scripts/migrate_diff_pages.py --data-dir ../g-i-t-data
      for every diff_<hash7>.html of the old kind that has no patch yet (the pages themselves are replaced by the next
      dashboard build of the job; nothing is deleted here)

  python scripts/migrate_diff_pages.py --data-dir ../g-i-t-data --hashes a84de5e,3783f15
  python scripts/migrate_diff_pages.py --data-dir ../g-i-t-data --hashes-file hashes.txt
      for the named commits (7+ characters), also where no page exists at all (pages lost with a container that
      was killed before it could commit them). Give only commits that have a row in `updates`: a patch makes a page.
      First snapshots (a commit that only adds files) are skipped.

Needs the full history of g-i-t-data (a normal clone; in a --filter=blob:none clone every commit is a download).
Then: git add public; git commit; git push (app first), and run the stalk job once.  --dry-run changes nothing.
"""
import argparse
import os
import subprocess
import sys

PATCH_SUFFIX = ".patch.txt"
DIFF_MARK_V3 = "<!--git-diff-v3-->"


def git(data_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=data_dir, capture_output=True)


def commit_slug(data_dir: str, commit: str):
    """(full hash, slug) of the commit, the slug being the one site directory (sites/<slug>/) it changes; or (None, reason).
    Any kind of commit that was reported can have a page: `Update <slug>`, and `Re-baseline` / `Reorder` ones that
    recover_rebaselined.py recovered."""
    res = git(data_dir, "show", "--format=%H", "--name-only", commit)
    if res.returncode != 0:
        return None, "commit not found"
    lines = res.stdout.decode("utf-8", "replace").splitlines()
    full, paths = lines[0].strip(), [l.strip() for l in lines[1:] if l.strip()]
    slugs = {path.split("/")[1] for path in paths if path.startswith("sites/") and path.count("/") >= 2}
    if len(slugs) != 1:
        return None, f"the commit changes {len(slugs)} sites"
    return full, slugs.pop()


def is_first_snapshot(data_dir: str, full: str, slug: str) -> bool:
    """A commit that only adds files is the first snapshot of a site: it has no diff page."""
    res = git(data_dir, "show", "--format=", "--name-status", full, "--", f"sites/{slug}")
    lines = [l for l in res.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    return bool(lines) and all(l.startswith("A") for l in lines)


def write_patch(data_dir: str, commit: str, slug: str, dry_run: bool) -> str:
    """'written' / 'exists' / 'empty'."""
    path = os.path.join(data_dir, "public", "sites", slug, f"diff_{commit[:7]}{PATCH_SUFFIX}")
    if os.path.isfile(path):
        return "exists"
    res = git(data_dir, "show", "--format=", "--patch", commit, "--", f"sites/{slug}")
    if res.returncode != 0 or not res.stdout.strip():
        return "empty"
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(res.stdout)
    return "written"


def old_pages(data_dir: str):
    """(slug, hash7) of every diff page that is not of the new kind."""
    root = os.path.join(data_dir, "public", "sites")
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, slug)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.startswith("diff_") and name.endswith(".html"):
                with open(os.path.join(folder, name), "r", encoding="utf-8", errors="replace") as f:
                    if DIFF_MARK_V3 not in f.read(300):
                        yield slug, name[5:-5]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="../g-i-t-data")
    p.add_argument("--hashes", default="", help="comma separated commit hashes (7+ characters)")
    p.add_argument("--hashes-file", default="", help="a file with one commit hash per line")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    data_dir = args.data_dir
    if not os.path.isdir(os.path.join(data_dir, ".git")):
        print(f"{data_dir} is not a git checkout.", file=sys.stderr)
        return 2

    counts = {"written": 0, "exists": 0, "empty": 0, "skipped": 0}
    problems = []
    named = [h.strip() for h in args.hashes.split(",") if h.strip()]
    if args.hashes_file:
        with open(args.hashes_file, encoding="utf-8") as f:
            named += [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if named:
        for commit in named:
            full, slug = commit_slug(data_dir, commit)
            if not full:
                counts["skipped"] += 1
                problems.append(f"{commit}: {slug}")
                continue
            if is_first_snapshot(data_dir, full, slug):
                counts["skipped"] += 1
                problems.append(f"{commit}: first snapshot of {slug}, no diff page")
                continue
            counts[write_patch(data_dir, full, slug, args.dry_run)] += 1
    else:
        for n, (slug, hash7) in enumerate(old_pages(data_dir), 1):
            result = write_patch(data_dir, hash7, slug, args.dry_run)
            counts[result] += 1
            if result == "empty":
                problems.append(f"{slug}/{hash7}: the commit has no patch for sites/{slug}")
            if n % 200 == 0:
                print(f"  {n} pages looked at ...", flush=True)

    print(("DRY RUN: " if args.dry_run else "") +
          f"patches written {counts['written']}, already there {counts['exists']}, no patch {counts['empty']}, skipped {counts['skipped']}.")
    for line in problems[:50]:
        print("  - " + line)
    if len(problems) > 50:
        print(f"  ... and {len(problems) - 50} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
