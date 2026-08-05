"""
Fútbol de Fantasía - Promoted Squad Scraper
One-time (per season) scraper for newly promoted clubs that have no player
records yet: discovers each club's WhoScored team ID from one of its
upcoming fixture pages, pulls the current squad from the team page, then
visits each player's profile for position eligibility.

Promoted clubs have no Premier League prior-season stats, so only squad +
eligibility is populated — their 2025-26 FPts will show 0.

Usage:
    python3 scrape_promoted_squads.py                # all promoted clubs
    python3 scrape_promoted_squads.py --club Hull    # one club
"""

import sqlite3
import argparse
import time
import re
from playwright.sync_api import sync_playwright
from init_db import DB_PATH

# short club name (as stored in fixtures) -> name fragments to match on the page
PROMOTED_CLUBS = {
    'Hull':     ['hull'],
    'Coventry': ['coventry'],
    'Ipswich':  ['ipswich'],
}


def classify_position(label):
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
    groups, depth, current = [], 0, ""
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


def dismiss_overlays(page):
    page.evaluate("""
        ['.webpush-swal2-container','#adm-sticky-snack-sticky','.gg-overlay-reset']
        .forEach(s => { const e=document.querySelector(s); if(e) e.remove(); });
    """)


def find_team_id(page, club, match_id):
    """Visit the club's upcoming fixture page and pull its /teams/{id}/ link."""
    for url_style in ('show', 'live'):
        url = f"https://www.whoscored.com/matches/{match_id}/{url_style}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            accept_cookies(page)
            dismiss_overlays(page)

            links = page.eval_on_selector_all(
                "a[href*='/teams/']",
                "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))"
            )
            fragments = PROMOTED_CLUBS[club]
            for link in links:
                m = re.search(r'/teams/(\d+)/', link['href'] or '')
                if not m:
                    continue
                haystack = (link['text'] + ' ' + link['href']).lower()
                if any(f in haystack for f in fragments):
                    return int(m.group(1))
        except Exception as e:
            print(f"    ⚠️  {url_style} page failed: {e}")
    return None


def scrape_squad(page, team_id):
    """Return {name: whoscored_id} for the club's current squad from its team page."""
    url = f"https://www.whoscored.com/teams/{team_id}/show/"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    accept_cookies(page)
    dismiss_overlays(page)

    squad = {}

    # Primary: the squad statistics table rows (same widget as match pages)
    try:
        page.wait_for_selector("#player-table-statistics-body tr", timeout=8000)
        rows = page.locator("#player-table-statistics-body tr").all()
        for row in rows:
            try:
                link = row.locator("a.player-link").first
                raw = link.inner_text(timeout=1500).strip()
                name = raw.split("\n")[-1].strip() if "\n" in raw else raw
                name = re.sub(r"^\d+\s*", "", name).strip()
                href = link.get_attribute("href") or ""
                m = re.search(r'/players/(\d+)/', href)
                if name and m:
                    squad[name] = int(m.group(1))
            except:
                continue
    except Exception:
        pass

    # Fallback: any player profile link on the page
    if not squad:
        links = page.eval_on_selector_all(
            "a[href*='/players/']",
            "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))"
        )
        for link in links:
            m = re.search(r'/players/(\d+)/', link['href'] or '')
            name = (link['text'] or '').strip()
            if m and name and len(name) > 2 and not name.isdigit():
                squad[name] = int(m.group(1))

    return squad


def scrape_profile_positions(page, whoscored_id):
    """Return (primary_position, positions_set) from a player profile page."""
    url = f"https://www.whoscored.com/players/{whoscored_id}/show/"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2.5)
    dismiss_overlays(page)

    row = page.locator("div:has(> span.info-label:has-text('Positions'))").first
    raw_text = row.inner_text(timeout=5000).strip()
    raw_text = re.sub(r'^Positions:\s*', '', raw_text)

    positions = []
    for g in split_top_level_commas(raw_text):
        main_label = re.sub(r'\(.*?\)', '', g).strip()
        pos = classify_position(main_label)
        if pos and pos not in positions:
            positions.append(pos)

    return (positions[0] if positions else None), set(positions)


def save_player(conn, name, club, whoscored_id, primary, positions):
    c = conn.cursor()
    existing = c.execute("SELECT id FROM players WHERE name=?", (name,)).fetchone()
    if existing:
        player_id = existing[0]
        c.execute("UPDATE players SET club=?, whoscored_id=? WHERE id=?",
                  (club, whoscored_id, player_id))
    else:
        c.execute("INSERT INTO players (name, club, position, whoscored_id) VALUES (?,?,?,?)",
                  (name, club, primary or 'MID', whoscored_id))
        player_id = c.lastrowid

    if primary:
        c.execute("UPDATE players SET position=? WHERE id=?", (primary, player_id))

    if positions:
        c.execute("DELETE FROM player_eligibility WHERE player_id=?", (player_id,))
        for pos in positions:
            c.execute(
                "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                (player_id, pos, 'whoscored_live')
            )
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--club", type=str, default=None, help="One club only (Hull/Coventry/Ipswich)")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--delay", type=float, default=2.5)
    args = parser.parse_args()

    clubs = [args.club] if args.club else list(PROMOTED_CLUBS.keys())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visible)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

        for club in clubs:
            print(f"\n{'='*50}\n{club}\n{'='*50}")

            fixture = c.execute(
                "SELECT match_id FROM fixtures WHERE season='2026-27' AND (home_club=? OR away_club=?) LIMIT 1",
                (club, club)
            ).fetchone()
            if not fixture:
                print(f"  ❌ No 2026-27 fixture found for {club} — skipping.")
                continue

            team_id = find_team_id(page, club, fixture[0])
            if not team_id:
                print(f"  ❌ Could not discover WhoScored team ID for {club} — skipping.")
                continue
            print(f"  Team ID: {team_id}")

            squad = scrape_squad(page, team_id)
            print(f"  Squad players found: {len(squad)}")
            if not squad:
                print(f"  ❌ Empty squad for {club} — page structure may have changed.")
                continue

            ok, failed = 0, 0
            for name, wsid in squad.items():
                try:
                    primary, positions = scrape_profile_positions(page, wsid)
                    if not positions:
                        raise ValueError("no positions parsed")
                    save_player(conn, name, club, wsid, primary, positions)
                    print(f"    ✅ {name}: {', '.join(sorted(positions))}")
                    ok += 1
                except Exception as e:
                    # Still save the player so they exist in the pool; eligibility can be trued up later
                    save_player(conn, name, club, wsid, None, set())
                    print(f"    ⚠️  {name}: saved without eligibility ({e})")
                    failed += 1
                time.sleep(args.delay)

            print(f"  {club} done: {ok} with eligibility, {failed} needing true-up.")

        browser.close()

    conn.close()
    print("\nAll done.")
