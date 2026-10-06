# syntax=docker/dockerfile:1
# G医t pipeline image: website-stalker + Python pipeline + diff2html + Firebase CLI.
#
# Settings are read from environment variables (GH_PAT, SUPABASE_URL, ...; see docs/cloud-run.md):
#   Cloud Run Job   --set-env-vars / --set-secrets (Secret Manager); the image contains no .env
#   local test      docker run --rm --env-file .env g-i-t-app            # stalk (default)
#                   docker run --rm --env-file .env g-i-t-app archive    # archive worker

FROM debian:trixie-slim AS stalker
# Rust comes from rustup (not from a distribution package or the rust image). "stable" is enough:
# the fork uses edition 2024 (Rust 1.85 or newer). Pin it with --build-arg RUST_TOOLCHAIN=1.xx.x.
ARG RUST_TOOLCHAIN=stable
# Empty = current HEAD of the fork; set a commit SHA to pin it.
ARG WS_REV=""
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain "$RUST_TOOLCHAIN"
RUN cargo install --locked --git https://github.com/TanukiMa/website-stalker.git ${WS_REV:+--rev $WS_REV} --root /out

FROM node:22-trixie-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 LANG=C.UTF-8 PIP_NO_CACHE_DIR=1
# website-stalker (reqwest 0.13) uses rustls, so no libssl is needed at run time; ca-certificates is.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 python3-venv tini \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g diff2html-cli firebase-tools && npm cache clean --force
COPY --from=stalker /out/bin/website-stalker /usr/local/bin/website-stalker

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt
ENV PATH=/opt/venv/bin:$PATH
COPY . .
RUN chmod +x container/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/container/entrypoint.sh"]
CMD ["stalk"]
