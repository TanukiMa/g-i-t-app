# syntax=docker/dockerfile:1
# G医t pipeline image: website-stalker + Python pipeline + diff2html + Firebase CLI.
#   docker build -t g-i-t-app .
#   docker run --rm --env-file .env g-i-t-app            # stalk (default)
#   docker run --rm --env-file .env g-i-t-app archive    # archive worker

FROM rust:1-bookworm AS stalker
# Empty = current HEAD of the fork; set a commit SHA to pin it.
ARG WS_REV=""
RUN cargo install --locked --git https://github.com/TanukiMa/website-stalker.git ${WS_REV:+--rev $WS_REV} --root /out

FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 LANG=C.UTF-8 PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates libssl3 python3 python3-venv tini \
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
