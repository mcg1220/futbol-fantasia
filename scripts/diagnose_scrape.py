"""
One-off diagnostic for the GW1 scrape failure: every match failed with
"TAB LOAD FAILURES" across every tab (including summary, the default tab),
for all 10 matches uniformly -- both symptoms point at the page never
actually rendering real content, not a per-match issue. This script hits
one match's live-stats page the same way scraper.py does and prints what's
actually on the page, so we can tell a Cloudflare/bot-detection block
(most likely for a cloud host's IP) apart from a genuine code bug.

Run via the Render Shell:
    cd scripts && python3 diagnose_scrape.py
"""
from playwright.sync_api import sync_playwright

MATCH_ID = 1983546  # Arsenal vs Coventry -- confirmed complete (3-0) via a normal residential IP
URL = f"https://www.whoscored.com/matches/{MATCH_ID}/livestatistics"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1600, "height": 1000},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        print(f"Navigating to {URL} ...")
        resp = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        print(f"HTTP status: {resp.status if resp else 'no response object'}")
        print(f"Final URL after redirects: {page.url}")
        print(f"Page title: {page.title()!r}")

        # Give any client-side challenge / lazy content a few seconds, same
        # as production would implicitly get via the retry/backoff already
        # in scraper.py -- we just want to see the state, not fix it here.
        page.wait_for_timeout(4000)

        body_text = page.inner_text("body")[:1500]
        print("\n── First 1500 chars of visible body text ──")
        print(body_text)

        html = page.content()
        print(f"\nTotal HTML length: {len(html)} chars")

        markers = {
            "cloudflare challenge": "Just a moment" in html or "cf-browser-verification" in html or "cf_chl" in html,
            "captcha": "captcha" in html.lower(),
            "access denied / forbidden": "access denied" in html.lower() or "403 forbidden" in html.lower(),
            "consent dialog text present": "consent" in html.lower() or "Accept" in html,
            "stat table container present": 'id="player-table-statistics-body"' in html,
            "tab nav present": "live-player-home-summary" in html,
        }
        print("\n── Signal check ──")
        for k, v in markers.items():
            print(f"  {'YES' if v else 'no '}  {k}")

        page.screenshot(path="diagnose_scrape_screenshot.png", full_page=False)
        print("\nSaved screenshot to scripts/diagnose_scrape_screenshot.png")

        browser.close()


if __name__ == '__main__':
    main()
