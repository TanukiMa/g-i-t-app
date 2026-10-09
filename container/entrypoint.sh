#!/usr/bin/env bash
# Entry point of the container.
#   stalk   (default)  clone g-i-t-data -> pipeline (fetch, summarize, push, build dashboard) -> deploy to Firebase Hosting
#   archive            drain archive_queue (scripts/archive_worker.py)
#   remake-dashboard   only rebuild the dashboard from the current data (after a change of templates / CSS / JS / build_dashboard.py):
#                      no fetching, no git commit or push, no AI; then deploy to DEPLOY_TARGETS
#   resummarize [args] regenerate stored AI summaries with net_diff() (scripts/resummarize.py; args are passed on, e.g. --commit 6e64f9f --limit 50 --model gemini-3.8-flash)
#   help | --help      list the modes (MODE --help: the details of one mode)
#   raw                editor experiment: fetch every site WITHOUT editors into g-i-t-data-raw (scripts/raw_experiment.py)
set -uo pipefail
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"   # set by bootstrap.sh (code fetched at start)
export HOME=/tmp   # Cloud Run's file system is read-only except /tmp; git and firebase write their config there

usage() {
  cat <<'HELP'
G-I-T container. Usage:  <image> MODE [options]      (MODE --help shows the details of one mode)

Modes:
  stalk               (default) Fetch every site in config.yaml with website-stalker, commit and push the changes
                      to g-i-t-data, store them in Supabase, write the AI summaries, rebuild the dashboard and
                      deploy it to DEPLOY_TARGETS.
  remake-dashboard    Only rebuild the dashboard from the current data, after a change of templates, CSS, JS or
                      build_dashboard.py. No fetching, no git commit or push, no AI. Then deploy.
  archive             Send the queued URLs (archive_queue) to the Internet Archive, rate limited.
  resummarize [opts]  Regenerate stored AI summaries from the commits of g-i-t-data (net changes only).
                      Options are those of scripts/resummarize.py: resummarize --help
  raw                 Editor experiment: fetch every site WITHOUT editors into g-i-t-data-raw.
                      Does nothing unless RAW_EXPERIMENT=1.
  help, --help, -h    This text.

Examples:
  wslc run --rm --env-file .env g-i-t-app --help
  wslc run --rm --env-file .env g-i-t-app stalk
  wslc run --rm --env-file .env g-i-t-app remake-dashboard
  wslc run --rm --env-file .env g-i-t-app resummarize --dry-run
  wslc run --rm --env-file .env g-i-t-app resummarize --commit 6e64f9f --model gemini-3.8-flash
HELP
}

mode_help() {
  case "$1" in
    stalk) cat <<'HELP'
stalk: the full pipeline (fetch, commit, push, database, AI summaries, dashboard, deploy).
Needs: GH_PAT SUPABASE_URL SUPABASE_KEY SITE_BASE_URL WEBSITE_STALKER_FROM, GEMINI_API_KEY (summaries),
       and what DEPLOY_TARGETS needs (github-pages | github-pages-redirect | firebase | cloudflare-pages).
Options (environment): STALK_BUDGET_MIN SUMMARY_WORKERS GEMINI_MODELS DATA_REPO APP_REF PWA_ENABLED ...
HELP
    ;;
    remake-dashboard) cat <<'HELP'
remake-dashboard: rebuild public/ from the stored data and deploy it. Nothing is fetched, committed or pushed, no AI is
called. The pages in g-i-t-data/public are rewritten by the next full "stalk" run.
Needs: GH_PAT (to clone g-i-t-data) SUPABASE_URL SUPABASE_KEY SITE_BASE_URL, and what DEPLOY_TARGETS needs.
HELP
    ;;
    archive) cat <<'HELP'
archive: register the URLs of archive_queue with the Internet Archive (Save Page Now).
Needs: SUPABASE_URL SUPABASE_KEY IA_ACCESS_KEY IA_SECRET_KEY
Options (environment): ARCHIVE_BATCH_SIZE ARCHIVE_INTERVAL_SEC ARCHIVE_RUNTIME_MIN ARCHIVE_ANON_FALLBACK ...
HELP
    ;;
    raw) cat <<'HELP'
raw: editor experiment. Fetch every site of config.yaml WITHOUT editors and commit the changes to g-i-t-data-raw.
Does nothing unless RAW_EXPERIMENT=1 (keep it 0 normally: every site is fetched a second time).
Needs: GH_PAT WEBSITE_STALKER_FROM    Options (environment): RAW_REPO RAW_WORKERS RAW_BUDGET_MIN
HELP
    ;;
    *) usage ;;
  esac
}

cmd="${1:-stalk}"
case "$cmd" in
  help|-h|--help) usage; exit 0 ;;
esac
if [ "$cmd" = "resummarize" ]; then
  case "${2:-}" in -h|--help) exec python "$APP_DIR/scripts/resummarize.py" --help ;; esac
elif [ "${2:-}" = "--help" ] || [ "${2:-}" = "-h" ] || [ "${2:-}" = "help" ]; then
  mode_help "$cmd"; exit 0
fi
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
elif [ "$cmd" = "resummarize" ]; then
  for v in GH_PAT SUPABASE_URL SUPABASE_KEY; do
    if [ -z "${!v:-}" ]; then echo "Missing environment variable: $v" >&2; exit 1; fi
  done
  cat > /tmp/askpass.sh <<'ASKPASS'
#!/bin/sh
case "$1" in Username*) echo x-access-token ;; *) echo "$GH_PAT" ;; esac
ASKPASS
  chmod +x /tmp/askpass.sh
  export GIT_ASKPASS=/tmp/askpass.sh GIT_TERMINAL_PROMPT=0
  mkdir -p /work && cd /work || exit 1
  git clone --filter=blob:none "https://github.com/${DATA_REPO:-TanukiMa/g-i-t-data}.git" data || exit 1   # full history: the commits are read
  shift
  exec python "$APP_DIR/scripts/resummarize.py" --data-dir ./data "$@"
elif [ "$cmd" != "stalk" ] && [ "$cmd" != "remake-dashboard" ]; then
  echo "Unknown mode: $cmd" >&2
  usage >&2
  exit 2
fi

# Where the dashboard is published: a comma separated list, every target is deployed independently.
#   github-pages      force-push data/public to the gh-pages branch of the data repository (the former stalk.yml step)
#   github-pages-redirect  the same branch, but every HTML page is a "we have moved" stub that sends the visitor to
#                     SITE_BASE_URL (the new address; scripts/make_redirect_site.py); feeds, status.json and
#                     search.json stay as they are, so feed readers and installed client apps keep working
#   firebase          Firebase Hosting                    needs FIREBASE_PROJECT
#   cloudflare-pages  Cloudflare Pages (wrangler)         needs CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_PAGES_PROJECT
DEPLOY_TARGETS="${DEPLOY_TARGETS:-github-pages}"
needed="GH_PAT SUPABASE_URL SUPABASE_KEY SITE_BASE_URL"
[ "$cmd" = "stalk" ] && needed="$needed WEBSITE_STALKER_FROM"   # build does not fetch
for t in ${DEPLOY_TARGETS//,/ }; do
  case "$t" in
    github-pages) ;;
    github-pages-redirect) ;;
    firebase) needed="$needed FIREBASE_PROJECT" ;;
    cloudflare-pages) needed="$needed CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_PAGES_PROJECT" ;;
    *) echo "Unknown DEPLOY_TARGETS entry: $t (github-pages, github-pages-redirect, firebase, cloudflare-pages)" >&2; exit 1 ;;
  esac
done
# A deploy tool that is not in this image (one image per target: container/Dockerfile --target firebase | cloudflare) fails now, not after the run.
for t in ${DEPLOY_TARGETS//,/ }; do
  case "$t" in
    firebase) command -v firebase >/dev/null 2>&1 || { echo "DEPLOY_TARGETS has firebase, but firebase-tools is not in this image (build container/Dockerfile with --target firebase)" >&2; exit 1; } ;;
    cloudflare-pages) command -v wrangler >/dev/null 2>&1 || { echo "DEPLOY_TARGETS has cloudflare-pages, but wrangler is not in this image (build container/Dockerfile with --target cloudflare)" >&2; exit 1; } ;;
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
if [ "$cmd" = "remake-dashboard" ]; then
  # Only the latest files are needed: public/ (with the diff pages) is read, the dashboard rewritten, nothing pushed.
  git clone --depth 1 "https://github.com/${DATA_REPO}.git" data || exit 1
  python "$APP_DIR/scripts/build_dashboard.py" --data-dir ./data --no-push
  status=$?
else
  git clone --filter=blob:none "https://github.com/${DATA_REPO}.git" data || exit 1

  python "$APP_DIR/scripts/website_stalk.py" --data-dir ./data
  status=$?

  git -C data gc --quiet || true
  echo "g-i-t-data .git size: $(du -sm data/.git | cut -f1) MB"
fi

# Publish a directory as the gh-pages branch of the data repository: a single commit, like force_orphan.
push_gh_pages() {
  local dir="$1"
  touch "$dir/.nojekyll" &&
  git -C "$dir" init -q -b gh-pages &&
  git -C "$dir" add -A &&
  git -C "$dir" commit -q -m "Deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)" &&
  git -C "$dir" push -q --force "https://github.com/${DATA_REPO}.git" gh-pages:gh-pages
}

deploy_github_pages() {
  local dir=/tmp/ghp
  rm -rf "$dir" && mkdir -p "$dir" && cp -a data/public/. "$dir/" && push_gh_pages "$dir"
}

# The old address after the move: stubs that send the visitor to SITE_BASE_URL (which must be the NEW address).
deploy_github_pages_redirect() {
  local dir=/tmp/ghp-redirect
  python "$APP_DIR/scripts/make_redirect_site.py" --src data/public --dst "$dir" --target "$SITE_BASE_URL" \
    --strip-prefix "/${DATA_REPO##*/}" && push_gh_pages "$dir"
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
