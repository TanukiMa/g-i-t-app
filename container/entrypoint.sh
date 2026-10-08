#!/usr/bin/env bash
# Entry point of the container.
#   stalk   (default)  clone g-i-t-data -> pipeline (fetch, summarize, push, build dashboard) -> deploy to Firebase Hosting
#   archive            drain archive_queue (scripts/archive_worker.py)
#   raw                editor experiment: fetch every site WITHOUT editors into g-i-t-data-raw (scripts/raw_experiment.py)
set -uo pipefail
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"   # set by bootstrap.sh (code fetched at start)
export HOME=/tmp   # Cloud Run's file system is read-only except /tmp; git and firebase write their config there

cmd="${1:-stalk}"
if [ "$cmd" = "archive" ]; then
  exec python "$APP_DIR/scripts/archive_worker.py"
elif [ "$cmd" = "raw" ]; then
  # Raw arm of the editor experiment (scripts/raw_experiment.py): the same sites without any editor, into g-i-t-data-raw.
  for v in GH_PAT WEBSITE_STALKER_FROM; do
    if [ -z "${!v:-}" ]; then echo "Missing environment variable: $v" >&2; exit 1; fi
  done
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
  git clone --depth 1 "https://github.com/${DATA_REPO:-TanukiMa/g-i-t-data}.git" data || exit 1   # only config.yaml is read
  git clone --filter=blob:none "https://github.com/${RAW_REPO:-TanukiMa/g-i-t-data-raw}.git" raw || exit 1
  python "$APP_DIR/scripts/raw_experiment.py" --data-dir ./data --raw-dir ./raw
  status=$?
  git -C raw gc --quiet || true
  echo "g-i-t-data-raw .git size: $(du -sm raw/.git | cut -f1) MB"
  exit "$status"
elif [ "$cmd" != "stalk" ]; then
  echo "usage: entrypoint.sh [stalk|archive|raw]" >&2
  exit 2
fi

# Where the dashboard is published: a comma separated list, every target is deployed independently.
#   github-pages      force-push data/public to the gh-pages branch of the data repository (the former stalk.yml step)
#   firebase          Firebase Hosting                    needs FIREBASE_PROJECT
#   cloudflare-pages  Cloudflare Pages (wrangler)         needs CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_PAGES_PROJECT
DEPLOY_TARGETS="${DEPLOY_TARGETS:-github-pages}"
needed="GH_PAT SUPABASE_URL SUPABASE_KEY WEBSITE_STALKER_FROM SITE_BASE_URL"
for t in ${DEPLOY_TARGETS//,/ }; do
  case "$t" in
    github-pages) ;;
    firebase) needed="$needed FIREBASE_PROJECT" ;;
    cloudflare-pages) needed="$needed CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_PAGES_PROJECT" ;;
    *) echo "Unknown DEPLOY_TARGETS entry: $t (github-pages, firebase, cloudflare-pages)" >&2; exit 1 ;;
  esac
done
# A deploy tool that is not in the image (see INSTALL_FIREBASE / INSTALL_WRANGLER in the Dockerfile) fails now, not after the run.
for t in ${DEPLOY_TARGETS//,/ }; do
  case "$t" in
    firebase) command -v firebase >/dev/null 2>&1 || { echo "DEPLOY_TARGETS has firebase, but this image was built without firebase-tools: rebuild with --build-arg INSTALL_FIREBASE=1" >&2; exit 1; } ;;
    cloudflare-pages) command -v wrangler >/dev/null 2>&1 || { echo "DEPLOY_TARGETS has cloudflare-pages, but this image was built without wrangler: rebuild with --build-arg INSTALL_WRANGLER=1" >&2; exit 1; } ;;
  esac
done
for v in $needed; do
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

python "$APP_DIR/scripts/website_stalk.py" --data-dir ./data
status=$?

git -C data gc --quiet || true
echo "g-i-t-data .git size: $(du -sm data/.git | cut -f1) MB"

deploy_github_pages() {
  local dir=/tmp/ghp
  rm -rf "$dir" && mkdir -p "$dir" && cp -a data/public/. "$dir/" && touch "$dir/.nojekyll" || return 1
  git -C "$dir" init -q -b gh-pages &&
  git -C "$dir" add -A &&
  git -C "$dir" commit -q -m "Deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)" &&
  git -C "$dir" push -q --force "https://github.com/${DATA_REPO}.git" gh-pages:gh-pages   # a single commit, like force_orphan
}

deploy_firebase() {
  cp "$APP_DIR/container/firebase.json" /work/firebase.json &&
  firebase deploy --only hosting --project "$FIREBASE_PROJECT" --non-interactive
}

deploy_cloudflare_pages() {
  WRANGLER_SEND_METRICS=false wrangler pages deploy data/public     --project-name "$CLOUDFLARE_PAGES_PROJECT" --branch "${CLOUDFLARE_PAGES_BRANCH:-main}" --commit-dirty=true
}

# Deploy whenever a dashboard exists, also after a partial failure (the exit status still reports it).
# One failing target does not stop the others.
if [ -f data/public/index.html ]; then
  for t in ${DEPLOY_TARGETS//,/ }; do
    echo "=== Deploy: $t ==="
    if "deploy_${t//-/_}"; then echo "Deploy $t: ok"; else echo "Deploy $t: FAILED" >&2; status=1; fi
  done
else
  echo "data/public/index.html is missing; skipping deploy." >&2
  status=1
fi
exit "$status"
