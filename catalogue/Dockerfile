# No browser, no Chromium, no queue broker: both primary sources serve complete
# JSON over plain HTTP, so nothing here needs a rendering engine.

# --- build stage: resolve dependencies into a self-contained venv -------------
FROM python:3.14-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /src

# Dependencies first, so source edits do not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# --- runtime stage: no uv, no build tooling ----------------------------------
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # uvicorn finds `app` via the working directory, but the `dramadex` console
    # script does not -- and CLI work is meant to run in here, not on the host.
    PYTHONPATH=/app \
    DRAMADEX_DB_URL=sqlite:////data/dramadex.db \
    DRAMADEX_DATA_DIR=/data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 dramadex \
    && mkdir -p /data \
    && chown -R dramadex:dramadex /data /app
USER dramadex

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

ARG DRAMATARR_REVISION=unknown
LABEL org.opencontainers.image.title="Dramatarr" \
      org.opencontainers.image.revision=$DRAMATARR_REVISION
