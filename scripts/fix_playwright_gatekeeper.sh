#!/bin/bash
# Fixes macOS Gatekeeper's "Malicious Script Blocked" dialog killing
# scrape_and_upload.py / scraper.py mid-run.
#
# Cause: Playwright downloads its own Chromium binary into
# ~/Library/Caches/ms-playwright, and macOS quarantines it like any other
# downloaded executable. Gatekeeper can (and does) re-flag it again after
# a future `playwright install` re-downloads a new build, so this may need
# to be re-run occasionally, not just once ever.
#
# Run this from an activated venv (same one you run the scraper from):
#   source venv/bin/activate
#   bash scripts/fix_playwright_gatekeeper.sh

set -e

echo "Clearing the quarantine flag on Playwright's cached browsers..."
xattr -cr ~/Library/Caches/ms-playwright

echo "Reinstalling Playwright's browser binaries..."
playwright install --force

echo "Done. Re-run your scrape now."
