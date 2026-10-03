"""Constants shared by website_stalk.py and build_dashboard.py."""

# Stored in updates.summary when no summary could be produced. SUMMARY_FAILED rows are retried
# by backfill_summaries(); SUMMARY_UNAVAILABLE marks rows whose diff can no longer be read.
SUMMARY_FAILED = "AI要約を生成できませんでした。"
SUMMARY_UNAVAILABLE = "差分を取得できなかったため要約できません。"
# First snapshot of a site: no AI call. The dashboard shows these entries muted.
SUMMARY_INITIAL = "初回取得: 監視を開始しました。次回以降の更新が差分として記録されます。"

DATA_REPO_URL = "https://github.com/TanukiMa/g-i-t-data"
