"""
Fútbol de Fantasía - Player Position Scraper
Pulls current position eligibility directly from a WhoScored player profile
page. WhoScored updates eligibility mid-season (tactical shifts, new roles,
etc.) — e.g. Casemiro gained DEF eligibility alongside MID during 2025-26 —
so this is meant to be re-run whenever you notice a discrepancy, or in bulk
via --all once whoscored_id has been captured for the whole squad.

Usage:
    # Manually correct one player using their WhoScored profile ID
    # (found in the URL: whoscored.com/players/{ID}/show/...)
    python3 scrape_player_positions.py --whoscored_id 88526 --player_name "Casemiro"

    # Re-scrape everyone who already has a stored whoscored_id
    python3 scrape_player_positions.py --all
"""

import sqlite3
import argparse
import time
import re
from playwright.sync_api import sync_playwright
from init_db import DB_PATH


def classify_position(label):
    """Map a WhoScored position label to our DEF/MID/FW/GK taxonomy."""
    label = label.lower()
    if 'goalkeeper' in label:
        return 'GK'
    if 'back' in label or 'defender' in label:
        return 'DEF'
    if 'midfielder' in label:
        return 'MID'
    if 'forward' in label or 'striker' in label:
        return 'FW'
    return None


def split_top_level_commas(text):
    """
    Split "Attacking Midfielder (Centre, Left), Forward" into top-level
    groups, ignoring commas that are inside parentheses.
    Returns: ["Attacking Midfielder (Centre, Left)", "Forward"]
    """
    groups = []
    depth = 0
    current = ""
    for ch in text:
        if ch == '(':
            depth += 1
            current += ch
        elif ch == ')':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            groups.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        groups.append(current.strip())
    return groups


def scrape_positions(whoscored_id, headless=True):
    """Visit a player's WhoScored profile and return their eligible positions as a set."""
    url = f"https://www.whoscored.com/players/{whoscored_id}/show/"
    positions = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

        try:
            print(f"  Loading {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            try:
                for text in ["Accept all", "Accept All", "Accept"]:
                    btn = page.locator(f"button:has-text('{text}')").first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        time.sleep(1)
                        break
            except:
                pass

            row = page.locator("div:has(> span.info-label:has-text('Positions'))").first
            raw_text = row.inner_text(timeout=5000).strip()
            raw_text = re.sub(r'^Positions:\s*', '', raw_text)
            print(f"  Raw positions text: '{raw_text}'")

            groups = split_top_level_commas(raw_text)
            for g in groups:
                main_label = re.sub(r'\(.*?\)', '', g).strip()
                pos = classify_position(main_label)
                if pos:
                    positions.add(pos)

        except Exception as e:
            print(f"  ❌ Error scraping player {whoscored_id}: {e}")
        finally:
            browser.close()

    return positions


def update_eligibility(player_id, positions):
    """Replace all eligibility rows for this player with the freshly scraped set."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM player_eligibility WHERE player_id=?", (player_id,))
    for pos in positions:
        c.execute(
            "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
            (player_id, pos, 'whoscored_live')
        )
    conn.commit()
    conn.close()


def run_for_player(name, whoscored_id, player_id, headless=True):
    print(f"\n[{name}] Scraping positions (whoscored_id={whoscored_id})...")
    positions = scrape_positions(whoscored_id, headless=headless)
    if not positions:
        print(f"  ⚠️  Could not determine positions for {name} — no changes made.")
        return
    update_eligibility(player_id, positions)
    print(f"  ✅ {name}: {', '.join(sorted(positions))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--whoscored_id", type=int, help="WhoScored player profile ID")
    parser.add_argument("--player_name",  type=str, help="Player name as it appears in our players table")
    parser.add_argument("--all", action="store_true", help="Re-scrape every player with a stored whoscored_id")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--delay", type=int, default=3)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if args.whoscored_id and args.player_name:
        row = c.execute("SELECT id FROM players WHERE name=?", (args.player_name,)).fetchone()
        if not row:
            print(f"No player named '{args.player_name}' found in players table.")
        else:
            player_id = row[0]
            # Persist the ID for future bulk re-scrapes
            c.execute("UPDATE players SET whoscored_id=? WHERE id=?", (args.whoscored_id, player_id))
            conn.commit()
            run_for_player(args.player_name, args.whoscored_id, player_id, headless=not args.visible)

    elif args.all:
        rows = c.execute("SELECT id, name, whoscored_id FROM players WHERE whoscored_id IS NOT NULL").fetchall()
        print(f"Found {len(rows)} players with known WhoScored IDs.")
        for player_id, name, whoscored_id in rows:
            run_for_player(name, whoscored_id, player_id, headless=not args.visible)
            time.sleep(args.delay)

    else:
        print("Usage:")
        print("  python3 scrape_player_positions.py --whoscored_id 88526 --player_name \"Casemiro\"")
        print("  python3 scrape_player_positions.py --all")

    conn.close()
