# Use Debian-based Node.js 20 LTS slim image
FROM node:20-bookworm-slim

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Base system dependencies: FFmpeg, cURL, Xvfb and the fonts/libraries Chrome needs
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    curl \
    ffmpeg \
    xvfb \
    xauth \
    dumb-init \
    fonts-liberation \
    fonts-freefont-ttf \
    fonts-ipafont-gothic \
    fonts-kacst \
    fonts-thai-tlwg \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxshmfence1 \
    procps \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp as a second download backend (the approach MeTube wraps). Debian's
# package lags badly and these CDNs change often, so install from PyPI.
RUN pip3 install --no-cache-dir --break-system-packages yt-dlp \
    && yt-dlp --version

# Install a browser. Google only ships Chrome for amd64, so arm64 builds
# (Apple Silicon, Raspberry Pi, Ampere) fall back to Debian's Chromium, which
# puppeteer-real-browser supports and which upstream itself uses.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" = "amd64" ]; then \
        mkdir -p /etc/apt/keyrings; \
        wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor -o /etc/apt/keyrings/googlechrome.gpg; \
        echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/googlechrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list; \
        apt-get update; \
        apt-get install -y --no-install-recommends google-chrome-stable; \
        ln -sf /usr/bin/google-chrome-stable /usr/local/bin/dramadown-browser; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends chromium; \
        ln -sf /usr/bin/chromium /usr/local/bin/dramadown-browser; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Browser & runtime defaults
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/dramadown-browser
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV NODE_ENV=production
ENV PORT=3000
# The server binds loopback by default; inside a container it must listen on all
# interfaces for the published port to reach it.
ENV HOST=0.0.0.0
ENV DOWNLOAD_DIR=/app/downloads
# Persisting the Chrome profile keeps the cf_clearance cookie between runs, so
# most extractions skip the Cloudflare challenge entirely.
ENV CHROME_USER_DATA_DIR=/app/.chrome-profile

WORKDIR /app

# Copy package descriptors first to leverage Docker cache
COPY package*.json ./

# Install from the lockfile for reproducible builds
RUN npm ci --omit=dev

# Copy application source code
COPY . .

# World-writable so the container can also be run as an unprivileged uid via
# compose's `user:` (e.g. user: "1000:1000") without any entrypoint gymnastics.
RUN mkdir -p /app/downloads /app/.chrome-profile /app/.home \
    && chmod 777 /app/downloads /app/.chrome-profile /app/.home
ENV HOME=/app/.home

# Xvfb gives Chrome a virtual display, so it runs *headful* with no physical
# screen. This is what makes the container both headless-to-the-operator and
# able to pass Cloudflare Turnstile — true --headless is far more detectable.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3000/api/health || exit 1

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["node", "server.js"]

ARG DRAMATARR_REVISION=unknown
LABEL org.opencontainers.image.title="Dramatarr" \
      org.opencontainers.image.revision=$DRAMATARR_REVISION
