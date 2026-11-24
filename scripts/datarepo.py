"""The layout of the data repository (g-i-t-data) as far as several scripts need it: where a change's patch lives, how it
is written, and how the first snapshot of a site is recognised in the history."""
import os

from common import PATCH_SUFFIX
from shell import git_out, git_silent


def patch_path(data_dir: str, slug: str, commit: str) -> str:
    """public/sites/<slug>/diff_<hash7>.patch.txt"""
    return os.path.join(data_dir, "public", "sites", slug, f"diff_{commit[:7]}{PATCH_SUFFIX}")


def commit_patch(commit_hash: str, data_dir: str, rel_site: str):
    """The raw patch of this site's part of a commit (what `git show` prints, as written by git), or None."""
    res = git_silent(data_dir, ["show", "--format=", "--patch", commit_hash, "--", rel_site], binary=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return res.stdout


def write_diff_patch(data_dir: str, site_slug: str, commit_hash: str):
    """Write public/sites/<slug>/diff_<hash7>.patch.txt, the record of a change. The page that shows it
    (diff_<hash7>.html: header, summary, diff2html in the browser) is made by build_dashboard.py. Returns the path or None."""
    patch = commit_patch(commit_hash, data_dir, f"sites/{site_slug}")
    if patch is None:
        return None
    path = patch_path(data_dir, site_slug, commit_hash)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(patch)
    return path


def is_first_snapshot(repo: str, commit: str, slug: str) -> bool:
    """The first fetch of a site adds every file (no parent version): it is never a change to judge and has no diff page."""
    out = git_out(repo, ["diff-tree", "--no-commit-id", "--name-status", "-r", "--no-renames", commit, "--", f"sites/{slug}"],
                  check=False)
    states = [l.split("\t", 1)[0] for l in out.splitlines() if l and not l.endswith("website-stalker.yaml")]
    return bool(states) and all(s == "A" for s in states)
