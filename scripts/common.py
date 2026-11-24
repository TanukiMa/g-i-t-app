"""Constants shared by website_stalk.py and build_dashboard.py (and the tools), plus env_flag()."""
import os


def env_flag(name: str) -> bool:
    """True when the environment variable is 1 / true / yes / on (any case); unset or anything else is False."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Stored in updates.summary when no summary could be produced. SUMMARY_FAILED rows are retried
# by backfill_summaries(); SUMMARY_UNAVAILABLE marks rows whose diff can no longer be read.
SUMMARY_FAILED = "要約を生成できませんでした。"
SUMMARY_UNAVAILABLE = "差分を取得できなかったため要約できません。"
# First snapshot of a site: no AI call. The dashboard shows these entries muted.
SUMMARY_INITIAL = "記録を開始しました"

DATA_REPO_URL = "https://github.com/TanukiMa/g-i-t-data"

# File name of a site's history page: public/sites/<slug>/history.html (index.html is reserved for the timelines).
SITE_PAGE = "history.html"

# The record of a change is public/sites/<slug>/diff_<hash7>.patch.txt (raw `git show`); diff_<hash7>.html is its view,
# marked with DIFF_MARK_V3 in its first bytes.
PATCH_SUFFIX = ".patch.txt"
DIFF_MARK_V3 = "<!--git-diff-v3-->"

# Absolute base URL of the published dashboard (needed for Atom feeds). Override with SITE_BASE_URL.
SITE_BASE_URL = "https://giiit.goudge.org/"

# When the pipeline runs (JST, "HH:MM,HH:MM,..."): published in feeds/status.json so that the client apps look for news
# shortly after a run instead of every 15 minutes. Cloud Scheduler decides the real times; keep this in step with it.
STALK_RUNS = "07:30,12:30,16:30"
STATUS_DELAY_MIN = 8     # a run is usually finished this many minutes after its start ...
STATUS_WINDOW_MIN = 25   # ... and the client looks for news during this many minutes after that

TIMELINE_LIMIT = 200   # entries on index / dashboard / minimal; older ones live in site/week/month pages
FEED_LIMIT_ALL = 100   # entries in the combined Atom feed
FEED_LIMIT_SITE = 50   # entries in per-site / per-tag Atom feeds
