"""Constants shared by website_stalk.py and build_dashboard.py."""

# Stored in updates.summary when no summary could be produced. SUMMARY_FAILED rows are retried
# by backfill_summaries(); SUMMARY_UNAVAILABLE marks rows whose diff can no longer be read.
SUMMARY_FAILED = "AI要約を生成できませんでした。"
SUMMARY_UNAVAILABLE = "差分を取得できなかったため要約できません。"
# First snapshot of a site: no AI call. The dashboard shows these entries muted.
SUMMARY_INITIAL = "初回取得: 監視を開始しました。次回以降の更新が差分として記録されます。"

DATA_REPO_URL = "https://github.com/TanukiMa/g-i-t-data"

# File name of a site's history page: public/sites/<slug>/history.html (index.html is reserved for the timelines).
SITE_PAGE = "history.html"

# Absolute base URL of the published dashboard (needed for Atom feeds). Override with SITE_BASE_URL.
SITE_BASE_URL = "https://tanukima.github.io/g-i-t-data/"

TIMELINE_LIMIT = 200   # entries on index / dashboard / minimal; older ones live in site/week/month pages
FEED_LIMIT_ALL = 100   # entries in the combined Atom feed
FEED_LIMIT_SITE = 50   # entries in per-site / per-tag Atom feeds
