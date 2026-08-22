FROM python:3.12-slim

WORKDIR /app

# Base tools patchright's own installer needs before it can pull in
# Chromium's OS-level dependencies (fonts, codecs, etc.) below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installs the actual Chromium browser binary the scraper drives, plus
# every OS-level library it needs to run headless — this is the whole
# reason this app needs a Docker image rather than a generic Python
# buildpack, which has no way to get a real browser onto the host.
# patchright (not plain playwright) — a patched build that avoids the
# CDP Runtime.enable leak most bot-detection (including WhoScored's
# Cloudflare challenge) uses to fingerprint automated Chromium.
RUN patchright install --with-deps chromium

COPY . .
RUN chmod +x entrypoint.sh

ENV PORT=5000
EXPOSE 5000

# entrypoint.sh symlinks data/ and static/uploads/ onto the host's single
# persistent disk (mounted at /var/data) before handing off to gunicorn —
# see that file for why.
ENTRYPOINT ["./entrypoint.sh"]

# One worker: Render's own sizing guidance for this instance (512MB RAM)
# already says WEB_CONCURRENCY=1, and the scraper's headless Chromium is
# memory-hungry enough on its own that a second full copy of the app
# loaded in a second worker process pushed a live run over the limit and
# OOM-crashed the container. 8 occasional users don't need a second
# worker for responsiveness — the scraper runs in a background thread
# either way, so it was never blocking other requests.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120", "app:app"]
