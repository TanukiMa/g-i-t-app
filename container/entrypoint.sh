#!/usr/bin/env bash
# Entry point of the container.
#   stalk   (default)  clone g-i-t-data -> pipeline (fetch, summarize, push, build dashboard) -> deploy to Firebase Hosting
#   archive            drain archive_queue (scripts/archive_worker.py)
set -uo pipefail
export HOME=/tmp   # Cloud Run's file system is read-only except /tmp; git and firebase write their config there

cmd="${1:-stalk}"
if [ "$cmd" = "archive" ]; then
  exec python /app/scripts/archive_worker.py
elif [ "$cmd" != "stalk" ]; then
  echo "usage: entrypoint.sh [stalk|archive]" >&2
  exit 2
fi

for v in GH_PAT SUPABASE_URL SUPABASE_KEY WEBSITE_STALKER_FROM FIREBASE_PROJECT SITE_BASE_URL; do
  if [ -z "${!v:-}" ]; then echo "Missing environment variable: $v" >&2; exit 1; fi
done
DATA_REPO="${DATA_REPO:-TanukiMa/g-i-t-data}"

# The token is handed to git on demand and never written into .git/config.
cat > /tmp/askpass.sh <<'ASKPASS'
#!/bin/sh
case "$1" in Username*) echo x-access-token ;; *) echo "$GH_PAT" ;; esac
ASKPASS
chmod +x /tmp/askpass.sh
export GIT_ASKPASS=/tmp/askpass.sh GIT_TERMINAL_PROMPT=0
git config --global user.name "g-i-t-bot"
git config --global user.email "g-i-t-bot@users.noreply.github.com"
git config --global core.quotepath false

mkdir -p /work && cd /work || exit 1
# Partial clone: full commit history, file contents (blobs) are fetched when needed.
git clone --filter=blob:none "https://github.com/${DATA_REPO}.git" data || exit 1

python /app/scripts/website_stalk.py --data-dir ./data
status=$?

git -C data gc --quiet || true
echo "g-i-t-data .git size: $(du -sm data/.git | cut -f1) MB"

# Deploy whenever a dashboard exists, also after a partial failure (the exit status still reports it).
if [ -f data/public/index.html ]; then
  cp /app/container/firebase.json /work/firebase.json
  firebase deploy --only hosting --project "$FIREBASE_PROJECT" --non-interactive || status=1
else
  echo "data/public/index.html is missing; skipping deploy." >&2
  status=1
fi
exit "$status"
