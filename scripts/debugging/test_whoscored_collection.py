from playwright.sync_api import sync_playwright
import time
import re

EPL_BASE_URL = "https://www.whoscored.com"
season_id = 11141
stages_id = 25544

def dismiss_overlays(page):
    page.evaluate("""
        ['.webpush-swal2-container','#adm-sticky-snack-sticky','.gg-overlay-reset']
        .forEach(s => { const e=document.querySelector(s); if(e) e.remove(); });
    """)

def accept_cookies(page, timeout=3000):
    try:
        for text in ["Accept All Cookies", "Accept All", "Accept all", "Accept"]:
            btn = page.locator(f"button:has-text('{text}')").first
            if btn.is_visible(timeout=timeout):
                btn.click()
                time.sleep(2)
                return True
    except:
        pass
    return False

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

    url = (f"{EPL_BASE_URL}/Regions/252/Tournaments/2/Seasons/{season_id}/"
           f"Stages/{stages_id}/fixtures/england-premier-league")
    print(f"Loading WhoScored fixtures page...")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)

    cookies_accepted = [False]
    if accept_cookies(page):
        cookies_accepted[0] = True

    dismiss_overlays(page)

    print("  Navigating to August...")
    for _ in range(12):
        try:
            month = page.locator(
                "button#toggleCalendar, button[class*='toggleCalendar']"
            ).first.inner_text(timeout=2000).strip()
            if "Aug" in month:
                print(f"  At: {month}")
                break
            dismiss_overlays(page)
            page.evaluate("document.getElementById('dayChangeBtn-prev').click()")
            time.sleep(1.5)
        except:
            break

    all_ids = []
    print("  Collecting match IDs month by month...")
    for _ in range(3):  # only test 3 months instead of 12
        try:
            month = page.locator(
                "button#toggleCalendar, button[class*='toggleCalendar']"
            ).first.inner_text(timeout=2000).strip()
        except:
            month = "?"

        html = page.content()
        ids = list(dict.fromkeys(re.findall(r'/matches/(\d+)/(?:live|show)', html)))
        new = [i for i in ids if i not in all_ids]
        all_ids.extend(new)
        print(f"  {month}: {len(new)} new (total {len(all_ids)})")

        try:
            dismiss_overlays(page)
            page.evaluate("document.getElementById('dayChangeBtn-next').click()")
            time.sleep(2)
        except:
            break

    print(f"\nTotal collected: {len(all_ids)}")
    print("Sample IDs:", all_ids[:10])
    browser.close()