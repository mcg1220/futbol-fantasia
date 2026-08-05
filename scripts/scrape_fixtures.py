"""
Fútbol de Fantasía - Fixture Scraper
Scrapes the full EPL fixture list from WhoScored and seeds the DB.

Approach:
1. Scrape matchweek → team pairs from the Premier League website (GW source of truth)
2. Scrape all match IDs + team names from WhoScored fixtures page (month by month)
3. Cross-reference team names to assign GW number
4. Seed fixtures and gameweeks tables
5. Validate every GW landed on exactly 10 fixtures

Usage:
    python3 scrape_fixtures.py --season_id 11141 --stages_id 25544 --season_str 2026-27 --pl_season 2026-27
"""

import sqlite3
import argparse
import time
import re
from playwright.sync_api import sync_playwright
from init_db import DB_PATH

EPL_BASE_URL   = "https://www.whoscored.com"
PL_FIXTURE_URL = "https://www.premierleague.com/en/matches/premier-league/{pl_season}/matchweek-{gw}"

TEAM_NAME_MAP = {
    "Leeds United":          "Leeds",
    "Manchester City":       "Manchester City",
    "Manchester United":     "Manchester United",
    "Newcastle United":      "Newcastle",
    "Nottingham Forest":     "Nottingham Forest",
    "Tottenham Hotspur":     "Tottenham",
    "West Ham United":       "West Ham",
    "Wolverhampton Wanderers": "Wolves",
    "Aston Villa":           "Aston Villa",
    "Arsenal":               "Arsenal",
    "Brentford":             "Brentford",
    "Brighton & Hove Albion": "Brighton",
    "Brighton and Hove Albion": "Brighton",
    "Burnley":               "Burnley",
    "Chelsea":               "Chelsea",
    "Crystal Palace":        "Crystal Palace",
    "Everton":               "Everton",
    "Fulham":                "Fulham",
    "Liverpool":             "Liverpool",
    "AFC Bournemouth":       "Bournemouth",
    "Bournemouth":           "Bournemouth",
    "Sunderland":            "Sunderland",
    "Ipswich Town":          "Ipswich",
    "Leicester City":        "Leicester",
    "Southampton":           "Southampton",
    "Hull City":             "Hull",
    "Coventry City":         "Coventry",
}


def normalize(name):
    return TEAM_NAME_MAP.get(name, name).lower().strip()


def get_or_create_gameweek(c, gw_number, season, is_playoff=0):
    row = c.execute("SELECT id FROM gameweeks WHERE gw_number=? AND season=?", (gw_number, season)).fetchone()
    if row:
        return row[0]
    c.execute(
        "INSERT INTO gameweeks (gw_number, season, status, is_playoff) VALUES (?,?,'complete',?)",
        (gw_number, season, is_playoff)
    )
    return c.lastrowid


def validate_gw_counts(conn, season, expected_per_gw=10):
    """
    Sanity check: every GW should have exactly `expected_per_gw` fixtures.
    Prints a warning for any GW that doesn't match — catches mis-assignment
    bugs (e.g. team-name matching errors) before they cause scoring issues.
    """
    c = conn.cursor()
    rows = c.execute("""
        SELECT g.gw_number, COUNT(*) as cnt
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season = ?
        GROUP BY g.gw_number
        ORDER BY g.gw_number
    """, (season,)).fetchall()

    problems = [(gw, cnt) for gw, cnt in rows if cnt != expected_per_gw]

    if problems:
        print(f"\n⚠️  WARNING: {len(problems)} gameweek(s) don't have exactly "
              f"{expected_per_gw} fixtures for {season}:")
        for gw, cnt in problems:
            print(f"    GW{gw}: {cnt} fixtures (expected {expected_per_gw})")
        print("    This usually means a fixture was matched to the wrong GW.")
        print("    Investigate before trusting scoring data for these GWs.\n")
    else:
        print(f"\n✅ All GWs for {season} have exactly {expected_per_gw} fixtures.\n")


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


# ── Step 1: Build GW → {(home, away)} lookup from PL website ──────────────────

def scrape_pl_matchweek(page, gw, pl_season, cookies_accepted):
    url = PL_FIXTURE_URL.format(pl_season=pl_season, gw=gw)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)

        if not cookies_accepted[0]:
            if accept_cookies(page):
                cookies_accepted[0] = True
                time.sleep(1)

        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except:
            pass

        fixtures = []
        cards = page.locator('[class*="match-card"]').all()
        for card in cards:
            try:
                home = card.locator(
                    '.match-card__team--home [data-testid="matchCardTeamFullName"]'
                ).first.inner_text(timeout=1000).strip()
                away = card.locator(
                    '.match-card__team--away [data-testid="matchCardTeamFullName"]'
                ).first.inner_text(timeout=1000).strip()
                if home and away:
                    fixtures.append((normalize(home), normalize(away)))
            except:
                continue

        return fixtures
    except Exception as e:
        print(f"    Warning scraping PL matchweek {gw}: {e}")
        return []


def build_gw_lookup(page, gw_start, gw_end, pl_season, cookies_accepted):
    lookup = {}
    print(f"\nBuilding GW lookup from Premier League website (GW{gw_start}–{gw_end})...")
    for gw in range(gw_start, gw_end + 1):
        fixtures = scrape_pl_matchweek(page, gw, pl_season, cookies_accepted)
        for home, away in fixtures:
            lookup[(home, away)] = gw
        print(f"  GW{gw}: {len(fixtures)} fixtures scraped")
        time.sleep(2)
    print(f"  Lookup contains {len(lookup)} fixtures total.")
    return lookup


# ── Step 2: Collect match IDs + team names from WhoScored fixtures page ───────

def collect_match_ids_from_whoscored(page, season_id, stages_id, cookies_accepted):
    url = (f"{EPL_BASE_URL}/Regions/252/Tournaments/2/Seasons/{season_id}/"
           f"Stages/{stages_id}/fixtures/england-premier-league")
    print(f"\nLoading WhoScored fixtures page...")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)

    if not cookies_accepted[0]:
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
    match_id_to_teams = {}
    print("  Collecting match IDs and team names month by month...")
    for _ in range(12):
        try:
            month = page.locator(
                "button#toggleCalendar, button[class*='toggleCalendar']"
            ).first.inner_text(timeout=2000).strip()
        except:
            month = "?"

        html   = page.content()
        ids    = list(dict.fromkeys(re.findall(r'/matches/(\d+)/(?:live|show)', html)))
        new    = [i for i in ids if i not in all_ids]
        all_ids.extend(new)

        for match_id in new:
            match_pos = html.find(f'/matches/{match_id}/')
            if match_pos == -1:
                continue

            context_start = max(0, match_pos - 800)
            context_end = min(len(html), match_pos + 800)
            context = html[context_start:context_end]

            team_pattern = r'<[^>]*>([A-Z][a-zA-Z\s\&\']+?)</[^>]*>'
            teams_found = re.findall(team_pattern, context)

            if len(teams_found) >= 2:
                home, away = teams_found[-2].strip(), teams_found[-1].strip()
                home = home.replace('&amp;', '&').strip()
                away = away.replace('&amp;', '&').strip()
                if home and away and len(home) > 2 and len(away) > 2:
                    match_id_to_teams[match_id] = (home, away)

        teams_found_count = len([m for m in new if m in match_id_to_teams])
        print(f"  {month}: {len(new)} new IDs, {teams_found_count} with team names extracted")

        if "May" in month:
            break

        try:
            dismiss_overlays(page)
            page.evaluate("document.getElementById('dayChangeBtn-next').click()")
            time.sleep(2)
        except:
            break

    return all_ids, match_id_to_teams


# ── Main ──────────────────────────────────────────────────────────────────────

def scrape_fixtures(season_id=10743, stages_id=24533, season_str="2025-26",
                    pl_season="2025-26", gw_start=1, gw_end=38):

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    existing_ids = set(r[0] for r in c.execute("SELECT match_id FROM fixtures").fetchall())
    print(f"DB has {len(existing_ids)} existing fixtures.")

    cookies_accepted = [False]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

        gw_lookup = build_gw_lookup(page, gw_start, gw_end, pl_season, cookies_accepted)

        all_ids, match_id_to_teams = collect_match_ids_from_whoscored(
            page, season_id, stages_id, cookies_accepted
        )
        print(f"\nTotal WhoScored match IDs: {len(all_ids)}")
        print(f"Team names extracted for: {len(match_id_to_teams)} matches")

        new_ids = [mid for mid in all_ids if int(mid) not in existing_ids]
        print(f"New (not in DB): {len(new_ids)}")

        if not new_ids:
            print("Nothing to add.")
            browser.close()
            conn.close()
            return

        print(f"\nMatching {len(new_ids)} new fixtures to GWs...")
        added = skipped = 0

        for i, match_id in enumerate(new_ids):
            mid = int(match_id)

            if match_id not in match_id_to_teams:
                print(f"  [{i+1}/{len(new_ids)}] ⚠️  {mid} — team names not found in fixture list")
                skipped += 1
                continue

            home_raw, away_raw = match_id_to_teams[match_id]
            home_norm = normalize(home_raw)
            away_norm = normalize(away_raw)

            gw_number = gw_lookup.get((home_norm, away_norm))
            if gw_number is None:
                print(f"  [{i+1}/{len(new_ids)}] ⚠️  {mid} — no GW match for "
                      f"'{home_raw}' vs '{away_raw}' "
                      f"(norm: '{home_norm}' vs '{away_norm}')")
                skipped += 1
                continue

            is_playoff = 1 if gw_number >= 34 else 0
            gw_id = get_or_create_gameweek(c, gw_number, season_str, is_playoff)
            c.execute(
                "INSERT OR IGNORE INTO fixtures (gw_id, match_id, home_club, away_club) "
                "VALUES (?,?,?,?)",
                (gw_id, mid, home_raw, away_raw)
            )
            conn.commit()
            added += 1
            print(f"  [{i+1}/{len(new_ids)}] GW{gw_number}: {home_raw} vs {away_raw} ✅")

        browser.close()

    print(f"\nDone. Added {added}, skipped {skipped}.")
    if added > 0:
        print("Run scrape_gw.py to scrape player stats for the new fixtures.")

    # Validate every GW landed on exactly 10 fixtures — catches mis-assignment
    # bugs immediately instead of silently corrupting downstream scoring data
    validate_gw_counts(conn, season_str)

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season_id",  type=int, default=10743)
    parser.add_argument("--stages_id",  type=int, default=24533)
    parser.add_argument("--season_str", type=str, default="2025-26")
    parser.add_argument("--pl_season",  type=str, default="2025-26")
    parser.add_argument("--gw_start",   type=int, default=1)
    parser.add_argument("--gw_end",     type=int, default=38)
    args = parser.parse_args()

    scrape_fixtures(
        season_id=args.season_id,
        stages_id=args.stages_id,
        season_str=args.season_str,
        pl_season=args.pl_season,
        gw_start=args.gw_start,
        gw_end=args.gw_end,
    )
