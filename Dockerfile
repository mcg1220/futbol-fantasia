FROM python:3.12-slim

WORKDIR /app

# Base tools playwright's own installer needs before it can pull in
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
RUN playwright install --with-deps chromium

COPY . .

ENV PORT=5000
EXPOSE 5000

# Two workers: enough headroom for 8-10 managers hitting the app
# occasionally, while staying small enough to keep SQLite write contention
# low — each gunicorn worker is a separate process, so more workers means
# more chances of two writes landing at the same instant even with WAL
# mode's improved concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
