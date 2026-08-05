"""
Fútbol de Fantasía - Player ID Harvester
Lightweight scraper that visits WhoScored match pages purely to extract each
player's WhoScored profile ID — no full stat scraping, only the "summary"
tab is clicked. Uses the same reliable Playwright locator approach as
scraper.py's scrape_team_tab() (proven across 48 matches with 0 discrepancies),
rather than raw regex on the page HTML, which was too brittle about exact
class-attribute matching.

Populates players.whoscored_id so scrape_player_positions.py --all can true
up eligibility broadly. Safe to re-run — only fills in IDs that are missing.

Usage:
    python3 harvest_player_ids.py --season 2025-26                    # every match this season
    python3 harvest_player_ids.py --season 2025-26 --gw 1 --gw_end 5  # a GW range
    python3 harvest_player_ids.py --season 2026-27
"""

import sqlite3
import argparse
import time
import re
from playwright.sync_api import sync_playwright
from init_db import DB_PATH


def get_match_ids(season, gw_start=None, gw_end=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if gw_start and gw_end:
        rows = c.execute("""
            SELECT f.match_id FROM fixtures f
            JOIN gameweeks g ON g.id = f.gw_id
            WHERE f.season=? AND g.gw_number BETWEEN ? AND ?
            ORDER BY f.match_id
        """, (season, gw_start, gw_end)).fetchall()
    else:
        rows = c.execute("""
            SELECT f.match_id FROM fixtures f
            JOIN gameweeks g ON g.id = f.gw_id
            WHERE f.season=?
            ORDER BY f.match_id
        """, (season,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def dismiss_overlays(page):
    page.evaluate("""
        ['.webpush-swal2-container','#adm-sticky-snack-sticky','.gg-overlay-reset']
        .forEach(s => { const e=document.querySelector(s); if(e) e.remove(); });
    """)


def accept_cookies(page):
    try:
        for text in ["Accept all", "Accept All", "Accept"]:
            btn = page.locator(f"button:has-text('{text}')").first
            if btn.is_visible(timeout=2000):
                btn.click()
                time.sleep(1)
                return
    except:
        pass


def click_tab(page, side, container_id, max_retries=2):
    """Click the summary tab and verify rows actually appear before reading content."""
    href = f"#live-player-{side}-summary"
    for attempt in range(1, max_retries + 1):
        try:
            page.evaluate(f"document.querySelector(\"a[href='{href}']\").click()")
            page.wait_for_selector(
                f"#{container_id} #player-table-statistics-body tr",
                timeout=6000
            )
            return True
        except Exception:
            if attempt < max_retries:
                time.sleep(2)
    return False


def extract_ids_from_container(page, container_id):
    """Same reliable approach as scraper.py's scrape_team_tab() — locator-based, not regex."""
    found = {}
    try:
        container = page.locator(f"#{container_id}")
        tbody = container.locator("#player-table-statistics-body")
        rows = tbody.locator("tr").all()

        for row in rows:
            try:
                link = row.locator("a.player-link").first
                raw_name = link.inner_text(timeout=2000).strip()
                name = raw_name.split("\n")[-1].strip() if "\n" in raw_name else raw_name
                name = re.sub(r"^\d+\s*", "", name).strip()

                href = link.get_attribute("href") or ""
                id_match = re.search(r'/players/(\d+)/', href)
                if name and id_match:
                    found[name] = int(id_match.group(1))
            except:
                continue
    except Exception as e:
        print(f"    ⚠️  Error reading {container_id}: {e}")

    return found


def harvest_match(page, match_id):
    """Visit one match page, click both teams' summary tabs, extract (name -> whoscored_id)."""
    url = f"https://www.whoscored.com/matches/{match_id}/livestatistics"
    found = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        accept_cookies(page)
        dismiss_overlays(page)

        home_ok = click_tab(page, "home", "statistics-table-home-summary")
        found.update(extract_ids_from_container(page, "statistics-table-home-summary"))

        away_ok = click_tab(page, "away", "statistics-table-away-summary")
        found.update(extract_ids_from_container(page, "statistics-table-away-summary"))

        if not home_ok and not away_ok:
            print(f"    ⚠️  Neither tab loaded for match {match_id}")

    except Exception as e:
        print(f"    ⚠️  Error on match {match_id}: {e}")

    return found


def update_whoscored_ids(name_to_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    updated = 0
    for name, wsid in name_to_id.items():
        c.execute(
            "UPDATE players SET whoscored_id=? WHERE name=? AND whoscored_id IS NULL",
            (wsid, name)
        )
        updated += c.rowcount
    conn.commit()
    conn.close()
    return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=str, required=True)
    parser.add_argument("--gw",     type=int, default=None)
    parser.add_argument("--gw_end", type=int, default=None)
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--delay", type=int, default=2)
    args = parser.parse_args()

    match_ids = get_match_ids(args.season, args.gw, args.gw_end)
    print(f"Harvesting player IDs from {len(match_ids)} matches ({args.season})...")

    total_updated = 0
    total_found = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visible)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

        for i, match_id in enumerate(match_ids):
            found = harvest_match(page, match_id)
            updated = update_whoscored_ids(found)
            total_updated += updated
            total_found += len(found)
            print(f"  [{i+1}/{len(match_ids)}] match_id={match_id} — found {len(found)}, new IDs saved {updated}")
            time.sleep(args.delay)

        browser.close()

    print(f"\nDone. Found {total_found} player references total, {total_updated} new whoscored_id values saved.")
    print("Run scrape_player_positions.py --all to true-up eligibility for everyone captured.")
