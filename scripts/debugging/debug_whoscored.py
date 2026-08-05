from playwright.sync_api import sync_playwright
import time
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    url = "https://www.whoscored.com/regions/252/tournaments/2/seasons/11141/stages/25544/fixtures/england-premier-league-2026-2027"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    try:
        btn = page.locator("button:has-text('Accept all')").first
        if btn.is_visible(timeout=2000):
            btn.click()
            time.sleep(2)
    except:
        pass

    print("PAGE TITLE:", page.title())

    try:
        month = page.locator("button#toggleCalendar, button[class*='toggleCalendar']").first.inner_text(timeout=3000)
        print("CALENDAR SHOWS:", month)
    except Exception as e:
        print("Could not find calendar button:", e)

    html = page.content()
    ids = re.findall(r'/matches/(\d+)/(?:live|show)', html)
    print("MATCH IDS FOUND ON CURRENT VIEW:", ids[:10], f"({len(ids)} total)")

    input("Press Enter to close browser...")
    browser.close()