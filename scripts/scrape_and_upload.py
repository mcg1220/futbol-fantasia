"""
Scrape a single WhoScored match locally and upload the results straight
into the live site's database over the network -- a stopgap for whenever
the hosted scraper can't reach WhoScored itself (see diagnose_scrape.py:
WhoScored's Cloudflare protection currently hard-blocks Render's IP).

Uses plain playwright with a normal desktop user-agent, not scraper.py's
patchright setup -- proven to work reliably from a residential IP, unlike
patchright here (see the GW1 diagnostic history for why). Reuses all of
scraper.py's actual parsing logic (TABS, click_tab, scrape_team_tab,
scrape_saves, etc.), just not its browser-launch step.

You need your own manager login (same one you use on the site) -- nobody
needs Render access for this. The site's /api/scrape/upload endpoint
accepts any logged-in manager's submission and writes it through the same
save_to_db() path a normal server-side scrape would use (idempotent --
safe to re-run, it skips players already saved for that match).

Usage:
    python3 scrape_and_upload.py --match_id 1983547 --gw 1 --manager_id 2
    python3 scrape_and_upload.py --match_id 1983547,1983548,1983549 --gw 1 --manager_id 2
    python3 scrape_and_upload.py --match_id 1983547 --gw 1 --manager_id 2 --site http://localhost:5050

--match_id accepts a comma-separated list to do several matches in one
run (one login, one at a time). You'll be prompted for your PIN (not
passed on the command line, so it doesn't end up in shell history).
"""
import argparse
import getpass
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.request

import scraper
from playwright.sync_api import sync_playwright

DEFAULT_SITE = "https://futbol-fantasia.onrender.com"


def scrape_match_plain_playwright(match_id):
    """Same flow as scraper.py's scrape_match(), but with plain playwright
    + a custom desktop UA instead of scraper.py's current patchright setup."""
    home_players, away_players = {}, {}
    home_team = away_team = ""
    goals_home = goals_away = 0
    tab_failures = []
    url = scraper.build_match_url(match_id)
    print(f"Scraping: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

            print("  Loading page...")
            if not scraper.goto_with_retry(page, url, timeout=60000, retries=2):
                raise Exception(f"Could not load match page after retries: {url}")
            time.sleep(5)

            scraper.accept_cookies(page)
            scraper.dismiss_overlays(page)

            home_team = page.locator(".home .team-link").first.inner_text(timeout=5000).strip()
            away_team = page.locator(".away .team-link").first.inner_text(timeout=5000).strip()
            print(f"  Teams: {home_team} vs {away_team}")

            score_text = page.locator(".result").first.inner_text(timeout=5000).strip()
            parts = re.split(r'[:\-]', score_text)
            goals_home = scraper.parse_int(parts[0])
            goals_away = scraper.parse_int(parts[1])
            print(f"  Score: {goals_home}-{goals_away}")

            for tab in scraper.TABS:
                print(f"  Scraping {tab} tab...")
                scraper.dismiss_overlays(page)

                home_container = f"statistics-table-home-{tab}"
                ok = scraper.click_tab(page, "home", tab, home_container)
                scraper.scrape_team_tab(page, home_container, home_team, home_players)
                if not ok:
                    tab_failures.append(f"home/{tab}")

                away_container = f"statistics-table-away-{tab}"
                ok = scraper.click_tab(page, "away", tab, away_container)
                scraper.scrape_team_tab(page, away_container, away_team, away_players)
                if not ok:
                    tab_failures.append(f"away/{tab}")

            print("  Scraping saves...")
            scraper.scrape_saves(page, match_id, home_team, away_team, home_players, away_players)
        finally:
            browser.close()

    all_players = list(home_players.values()) + list(away_players.values())
    print(f"  Scraped {len(home_players)} home, {len(away_players)} away players.")
    if tab_failures:
        print(f"  ⚠️⚠️  TAB LOAD FAILURES: {', '.join(tab_failures)}")

    return all_players, home_team, away_team, goals_home, goals_away


def login(opener, site, manager_id, pin):
    req = urllib.request.Request(
        f"{site}/login", method="POST",
        data=json.dumps({"manager_id": manager_id, "pin": pin}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Login failed: {e.read().decode()}")
        sys.exit(1)


UPLOAD_PLAYER_FIELDS = set(scraper.empty_player("", "").keys()) | {"player_id"}


def clean_players_for_upload(players):
    """Strip internal-only parsing state (e.g. parse_incidents' _seen_incidents
    set, not JSON-serializable and not a raw_stats column) down to just the
    fields the upload endpoint actually accepts."""
    return [{k: p[k] for k in UPLOAD_PLAYER_FIELDS if k in p} for p in players]


def upload(opener, site, match_id, gw, home_team, away_team, goals_home, goals_away, players):
    players = clean_players_for_upload(players)
    payload = {
        "match_id": match_id, "gw": gw,
        "home_team": home_team, "away_team": away_team,
        "goals_home": goals_home, "goals_away": goals_away,
        "players": players,
    }
    req = urllib.request.Request(
        f"{site}/api/scrape/upload", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Upload failed: {e.read().decode()}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match_id", type=str, required=True,
                     help="One match_id, or a comma-separated list (e.g. 1983546,1983548) to do several in one run")
    ap.add_argument("--gw", type=int, required=True)
    ap.add_argument("--manager_id", type=int, required=True, help="Your manager id (same as the login dropdown)")
    ap.add_argument("--site", type=str, default=DEFAULT_SITE)
    args = ap.parse_args()

    match_ids = [int(m) for m in str(args.match_id).split(',') if m.strip()]

    pin = getpass.getpass("PIN: ")

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    print(f"Logging in to {args.site} as manager {args.manager_id}...")
    login(opener, args.site, args.manager_id, pin)
    print("Logged in.")

    succeeded, failed = [], []
    for i, match_id in enumerate(match_ids, start=1):
        print(f"\n[{i}/{len(match_ids)}] match_id={match_id}")
        try:
            players, home_team, away_team, goals_home, goals_away = scrape_match_plain_playwright(match_id)
            if len(players) < scraper.MIN_EXPECTED_PLAYERS:
                print("  Scrape failed (not enough players captured) -- not uploading this one.")
                failed.append(match_id)
                continue
            print(f"  Uploading to {args.site}...")
            result = upload(opener, args.site, match_id, args.gw, home_team, away_team,
                             goals_home, goals_away, players)
            print(f"  Done: {result}")
            succeeded.append(match_id)
        except Exception as e:
            print(f"  Error on match {match_id}: {e}")
            failed.append(match_id)

    print(f"\n{len(succeeded)}/{len(match_ids)} match(es) uploaded successfully.")
    if failed:
        print(f"Failed/skipped: {failed}")
        sys.exit(1)


if __name__ == '__main__':
    main()
