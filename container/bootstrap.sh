#!/usr/bin/env bash
# Baked into the image. The application code is NOT in the image: it is fetched here on every start, so a
# change of scripts/templates/static only needs a push, not a new image. Only dependencies live in the image.
#   APP_REPO  (default TanukiMa/g-i-t-app, public)
#   APP_REF   (default main) branch, tag or commit SHA; pin it to roll back without rebuilding:
#             gcloud run jobs update g-i-t-stalk --update-env-vars APP_REF=<good commit>
# Without network access to the code, e.g. for a local test, mount a checkout at /app (-v "$PWD:/app") and it is used as it is.
set -euo pipefail
export HOME=/tmp

if [ -f /app/container/entrypoint.sh ]; then
  app=/app
  echo "Using the application code mounted at /app."
else
  APP_REPO="${APP_REPO:-TanukiMa/g-i-t-app}"
  APP_REF="${APP_REF:-main}"
  app=/tmp/app
  rm -rf "$app"
  git init --quiet "$app"
  git -C "$app" fetch --quiet --depth 1 "https://github.com/${APP_REPO}.git" "$APP_REF"
  git -C "$app" checkout --quiet FETCH_HEAD
  echo "Application code: ${APP_REPO}@$(git -C "$app" rev-parse --short HEAD) (ref ${APP_REF})"
fi

# The image holds the dependencies of the requirements.txt it was built with. A different one needs a new image.
if ! diff -q <(tr -d '\r' < /opt/requirements.baked.txt) <(tr -d '\r' < "$app/requirements.txt") >/dev/null; then
  echo "requirements.txt differs from the one this image was built with. Rebuild the image (docs/cloud-run.md, step 4)" >&2
  echo "or pin APP_REF to a commit that matches it." >&2
  exit 3
fi

export APP_DIR="$app"
# bash, not exec of the file: a file checked out from GitHub may lack the executable bit (it was added on Windows)
exec bash "$app/container/entrypoint.sh" "$@"
