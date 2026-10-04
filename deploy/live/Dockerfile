# ewbot live runtime (Plan 3). Built on the VPS from a tag on master by deploy/live/deploy.sh:
#   docker build -f deploy/live/Dockerfile -t ewbot-live:<tag> .
# Only pyproject.toml, uv.lock, src/ and holdout.lock enter the build (Dockerfile.dockerignore); no
# secret is ever copied in: the CDP key and the secrets file are mounted read-only at run time.
# The project is installed editable at /app, so the fingerprint finds /app/src/ewbot and the
# committed /app/holdout.lock (ewbot live preflight checks both).
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /bin/uv

# UV_NO_CACHE: uv's download cache (~300 MB) never stays in a layer of the image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    EWBOT_LIVE_DIR=/data \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group live --no-install-project
COPY src ./src
COPY holdout.lock ./
# The root file system is read-only at run time: /app/src is byte-compiled here (the venv by UV_COMPILE_BYTECODE),
# else every `ewbot` process - the health check every minute included - compiles it again. The fingerprint hashes the
# *.py files only.
RUN uv sync --frozen --no-dev --group live \
    && python -m compileall -q /app/src \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin ewbot \
    && mkdir -p /data \
    && chown ewbot:ewbot /data

# Verified TLS (the price feed's WebSocket checks Coinbase's certificate against the system CA store) needs
# the base image's ca-certificates: fail the build, not the feed, if a new base image lacks them.
RUN test -s /etc/ssl/certs/ca-certificates.crt

USER ewbot
# `docker stop` sends SIGTERM: `ewbot live run` finishes its current tick, stops its threads, flushes the queued
# Telegram messages and exits 0 (no [중단], no /fail). Compose runs it under an init (`init: true`) that hands the
# signal on, and its stop_grace_period leaves time for the tick and the flush.
STOPSIGNAL SIGTERM
# Unhealthy = the loop's heartbeat (journaled every minute) is older than 3 minutes: a hung loop. `--json` answers
# from the journal alone (no gates computed). Docker never restarts an unhealthy container: see the runbook.
HEALTHCHECK --interval=60s --timeout=30s --start-period=300s --retries=3 \
    CMD ["ewbot", "live", "status", "--json", "--max-age", "180"]
ENTRYPOINT ["ewbot", "live"]
CMD ["run"]
