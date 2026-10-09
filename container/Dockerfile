# syntax=docker/dockerfile:1
# G醫t pipeline image: website-stalker + Python dependencies + diff2html + the CLI of ONE deploy target.
# The image holds only tools and dependencies; the code of g-i-t-app is fetched at start (container/bootstrap.sh).
#
# Build from the repository root (the build context), choose the target with --target:
#   --target firebase     Cloud Run + Firebase Hosting (+ GitHub Pages)   firebase-tools   about +250 MB   (default)
#   --target cloudflare   Cloudflare Pages (+ GitHub Pages)               wrangler         about +170 MB
#   --target base         GitHub Pages only (no extra CLI)
#   docker build -f container/Dockerfile --target firebase -t g-i-t-app:firebase .
#   wslc   build -f container/Dockerfile --target cloudflare -t g-i-t-app:cloudflare .
#   gcloud builds submit --config cloudbuild.yaml --substitutions _IMAGE=$IMAGE,_TARGET=firebase .
# DEPLOY_TARGETS (environment variable) must only name targets whose CLI is in the image; the container checks it at start.
#
# Settings are read from environment variables (GH_PAT, SUPABASE_URL, ...; see docs/cloud-run.md):
#   Cloud Run Job   --set-env-vars / --set-secrets (Secret Manager); the image contains no .env
#   local test      docker run --rm --env-file .env g-i-t-app:firebase            # stalk (default)
#                   docker run --rm --env-file .env g-i-t-app:firebase archive    # archive worker
#                   add -v "$PWD:/app" to run your working copy instead of fetching the code from GitHub

FROM debian:trixie-slim AS stalker
# rustup is installed with apt (Debian's own package, signed by the distribution; no "curl | sh"). It provides
# the cargo/rustc proxies in /usr/bin; the toolchain itself is then fetched by rustup. "stable" is enough:
# the fork uses edition 2024 (Rust 1.85 or newer). Pin it with --build-arg RUST_TOOLCHAIN=1.xx.x.
ARG RUST_TOOLCHAIN=stable
# Empty = current HEAD of the fork; set a commit SHA to pin it.
ARG WS_REV=""
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git build-essential pkg-config rustup \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install "$RUST_TOOLCHAIN" --profile minimal && rustup default "$RUST_TOOLCHAIN"
RUN cargo install --locked --git https://github.com/TanukiMa/website-stalker.git ${WS_REV:+--rev $WS_REV} --root /out

# ---- base: everything the pipeline needs except a deploy CLI (measured: about 480 MB) ----
FROM node:22-trixie-slim AS base
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 LANG=C.UTF-8 PIP_NO_CACHE_DIR=1
# website-stalker (reqwest 0.13) uses rustls, so no libssl is needed at run time; ca-certificates is.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 python3-venv tini \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN npm install -g diff2html-cli && npm cache clean --force
COPY --from=stalker /out/bin/website-stalker /usr/local/bin/website-stalker

# Dependencies only. The application code (scripts, templates, static, container/entrypoint.sh) is fetched
# from GitHub on every start by bootstrap.sh; rebuild this image only when requirements.txt or this file changes.
COPY requirements.txt /opt/requirements.baked.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -r /opt/requirements.baked.txt
ENV PATH=/opt/venv/bin:$PATH
COPY container/bootstrap.sh /usr/local/bin/bootstrap.sh
RUN chmod +x /usr/local/bin/bootstrap.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bootstrap.sh"]
CMD ["stalk"]

# ---- cloudflare: Cloudflare Pages (wrangler pages deploy) ----
# Unpinned = the version of the day the image is built; pin it for a reproducible rebuild: --build-arg WRANGLER_VERSION=x.y.z
FROM base AS cloudflare
ARG WRANGLER_VERSION=latest
RUN npm install -g wrangler@${WRANGLER_VERSION} && npm cache clean --force

# ---- firebase: Firebase Hosting (firebase deploy). The LAST stage, so it is what a build without --target gives. ----
FROM base AS firebase
ARG FIREBASE_TOOLS_VERSION=latest
RUN npm install -g firebase-tools@${FIREBASE_TOOLS_VERSION} && npm cache clean --force
