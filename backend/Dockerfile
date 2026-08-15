FROM python:3.12-slim

# ffmpeg/ffprobe are load-bearing, not optional: ffprobe drives the scanner's media analysis
# and ffmpeg converts subtitles to WebVTT and builds scrubbing thumbnails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# The app runs unprivileged. /library is mounted read-only and /data must be writable by
# this uid — compose creates /data as a named volume so ownership is handled there.
RUN useradd --create-home --uid 1000 dramatarr \
    && mkdir -p /data \
    && chown -R dramatarr:dramatarr /data /app
USER dramatarr

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
