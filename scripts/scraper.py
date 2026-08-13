"""
Fútbol de Fantasía - WhoScored Scraper
Scrapes player stats from a WhoScored match page.

Usage:
    python3 scraper.py --match_id 1903173 --gw 5 --visible --compare
    python3 scraper.py --match_id 1903173 --gw 5
    python3 scraper.py --match_id 1903173 --gw 5 --rescrape
"""

import sqlite3
import argparse
import sys
import time
import re
from playwright.sync_api import sync_playwright
from init_db import DB_PATH

# ── Confirmed stat cell class names from WhoScored HTML inspection ─────────────
STAT_CLASS_MAP = {
    "ShotOnTarget":         "shots_on_target",
    "KeyPassTotal":         "key_passes",
    "DribbleWon":           "dribbles",
    "TackleWonTotal":       "tackles",
    "InterceptionAll":      "interceptions",
    "ClearanceTotal":       "clearances",
    "ShotBlocked":          "blocked_shots",
    "PassCrossAccurate":    "acc_crosses",
    "PassLongBallAccurate": "acc_long_balls",
    "SaveTotal":            "saves",
}

INCIDENT_TYPE_MAP = {
    "16": "goals",
    "1":  "assists",
    "13": "assists",
    "15": "assists",
    "61": "assists",
    "74": "assists",
    "2":  "own_goals",
    "5":  "red_cards",
}

# data-type="7" is ambiguous on WhoScored — it's reused for last-man-tackle
# incidents too, so it only counts as an assist when the same incident also
# carries the "assistother" satisfier. Handled separately from
# INCIDENT_TYPE_MAP below rather than mapped directly, to avoid falsely
# crediting every last-man-tackle as an assist (LMT is tracked independently
# via SATISFIER_STAT_MAP's "tacklelastman" satisfier).
AMBIGUOUS_ASSIST_TYPE = "7"

SATISFIER_STAT_MAP = {
    "yellowcard":           "yellow_cards",
    "secondyellow":         "second_yellow",
    "redcard":              "red_cards",
    "keeperpenaltysaved":   "pk_saves",
    "clearanceofftheline":  "glc",
    "tacklelastman":        "lmt",
    "errorleadstogoal":     "elg",
}

KNOWN_GK_IDS = set()

TABS = ["summary", "offensive", "defensive", "passing"]


def build_match_url(match_id):
    return f"https://www.whoscored.com/matches/{match_id}/livestatistics"


def parse_int(val):
    if val is None:
        return 0
    val = str(val).strip()
    if val in ("", "-", "N/A"):
        return 0
    try:
        return int(float(val))
    except:
        return 0


def goto_with_retry(page, url, timeout=60000, retries=2, wait_until="domcontentloaded"):
    """
    Navigate to a URL with automatic retry on timeout/network errors.
    WhoScored occasionally times out on a single request even when otherwise
    healthy — this retries before giving up, rather than failing the whole match.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"    ⚠️  Page load attempt {attempt}/{retries} failed ({type(e).__name__}), retrying in 5s...")
                time.sleep(5)
    print(f"    ❌ Page load failed after {retries} attempts: {last_error}")
    return False


def empty_player(name, club):
    return {
        "player_name": name, "club": club,
        "goals": 0, "assists": 0, "shots_on_target": 0, "key_passes": 0,
        "dribbles": 0, "tackles": 0, "interceptions": 0, "clearances": 0,
        "blocked_shots": 0, "saves": 0, "acc_crosses": 0, "acc_long_balls": 0,
        "own_goals": 0, "yellow_cards": 0, "red_cards": 0, "motm": 0,
        "pk_saves": 0, "glc": 0, "lmt": 0, "elg": 0,
        "sub_on_min": 0, "sub_off_min": 0, "minutes_played": 90,
    }


def parse_incidents(row_html, player):
    if 'data-mom=""' in row_html:
        player["motm"] = 1

    suboff = re.search(r'data-minute="(\d+)"[^>]*data-event-satisfier-suboff=""', row_html)
    if not suboff:
        suboff = re.search(r'data-event-satisfier-suboff=""[^>]*data-minute="(\d+)"', row_html)
    if suboff:
        min_off = int(suboff.group(1))
        player["sub_off_min"] = min_off
        player["minutes_played"] = min_off

    subon = re.search(r'data-minute="(\d+)"[^>]*data-event-satisfier-subon=""', row_html)
    if not subon:
        subon = re.search(r'data-event-satisfier-subon=""[^>]*data-minute="(\d+)"', row_html)
    if subon:
        min_on = int(subon.group(1))
        player["sub_on_min"] = min_on
        player["minutes_played"] = 90 - min_on

    seen = player.setdefault("_seen_incidents", set())
    for m in re.finditer(r'<span class="incident-icon"([^>]+)>', row_html):
        attrs = m.group(1)
        min_match = re.search(r'data-minute="(\d+)"', attrs)
        type_match = re.search(r'data-type="(\d+)"', attrs)
        if not min_match or not type_match:
            continue
        minute, data_type = min_match.group(1), type_match.group(1)
        key = (minute, data_type)
        if key in seen:
            continue
        seen.add(key)
        if data_type == AMBIGUOUS_ASSIST_TYPE:
            db_col = "assists" if "data-event-satisfier-assistother" in attrs else None
        else:
            db_col = INCIDENT_TYPE_MAP.get(data_type)
        if db_col:
            player[db_col] = player.get(db_col, 0) + 1

    has_second_yellow = "secondyellow" in row_html
    seen_sat = player.setdefault("_seen_satisfiers", set())
    # Counts (satisfier, minute) occurrences within THIS parse call only, so
    # two genuinely distinct events of the same type in the same minute (e.g.
    # two separate last-man-tackle incidents) each get their own dedup key
    # instead of collapsing into one. Deterministic re-parses of the same
    # row_html (this function runs once per tab scraped) walk the spans in
    # the same order, so the occurrence index -- and therefore the key -- is
    # identical across calls, keeping cross-tab dedup intact.
    local_occurrence = {}

    for incident_match in re.finditer(r'<span class="incident-icon"([^>]+)>', row_html):
        attrs = incident_match.group(1)
        min_match = re.search(r'data-minute="(\d+)"', attrs)
        minute = min_match.group(1) if min_match else "0"

        for satisfier, db_col in SATISFIER_STAT_MAP.items():
            if f'data-event-satisfier-{satisfier}=""' not in attrs:
                continue
            if satisfier in ("yellowcard", "secondyellow", "redcard"):
                key = satisfier
            else:
                occurrence = local_occurrence.get((satisfier, minute), 0)
                local_occurrence[(satisfier, minute)] = occurrence + 1
                key = (satisfier, minute, occurrence)
            if key in seen_sat:
                continue
            seen_sat.add(key)
            if satisfier == "secondyellow":
                continue
            if satisfier == "yellowcard" and has_second_yellow:
                continue
            if db_col:
                player[db_col] = player.get(db_col, 0) + 1

    seen_og = player.setdefault("_seen_own_goals", set())
    for m in re.finditer(r'data-minute="(\d+)"[^>]*data-type="16"[^>]*data-event-satisfier-goalown=""', row_html):
        minute = m.group(1)
        if minute not in seen_og and (minute, "16") in seen:
            seen_og.add(minute)
            player["goals"] = max(0, player.get("goals", 0) - 1)
            player["own_goals"] = player.get("own_goals", 0) + 1
    for m in re.finditer(r'data-event-satisfier-goalown=""[^>]*data-minute="(\d+)"', row_html):
        minute = m.group(1)
        if minute not in seen_og and (minute, "16") in seen:
            seen_og.add(minute)
            player["goals"] = max(0, player.get("goals", 0) - 1)
            player["own_goals"] = player.get("own_goals", 0) + 1

    if player.get("sub_on_min", 0) > 0 and player.get("sub_off_min", 0) > 0:
        player["minutes_played"] = player["sub_off_min"] - player["sub_on_min"]


def scrape_team_tab(page, container_id, club, players):
    """Scrape one tab container for one team. Updates players dict in place."""
    try:
        container = page.locator(f"#{container_id}")
        tbody = container.locator("#player-table-statistics-body")
        rows = tbody.locator("tr").all()

        for row in rows:
            try:
                raw = row.locator("a.player-link").first.inner_text(timeout=2000).strip()
                name = raw.split("\n")[-1].strip() if "\n" in raw else raw
                name = re.sub(r"^\d+\s*", "", name).strip()
            except:
                continue
            if not name:
                continue

            if name not in players:
                players[name] = empty_player(name, club)

            try:
                href = row.locator("a.player-link").first.get_attribute("href") or ""
                id_match = re.search(r'/players/(\d+)/', href)
                player_id = int(id_match.group(1)) if id_match else None
                if player_id:
                    players[name]["player_id"] = player_id

                meta = row.locator(".player-meta-data").all()
                for m in meta:
                    txt = m.inner_text().strip().strip(",").strip()
                    if txt in ("GK", "DC", "DR", "DL", "MC", "MR", "ML",
                               "AMC", "FW", "DMC", "DML", "DMR", "AMR", "AML"):
                        players[name]["position"] = txt
                        if txt == "GK" and player_id:
                            KNOWN_GK_IDS.add(player_id)
                        break
                    elif txt == "Sub" and player_id and player_id in KNOWN_GK_IDS:
                        players[name]["position"] = "GK"
                        break
            except:
                pass

            try:
                row_html = row.inner_html(timeout=2000)
                parse_incidents(row_html, players[name])
            except:
                pass

            cells = row.locator("td").all()
            for cell in cells:
                try:
                    cls = cell.get_attribute("class") or ""
                    for stat_class, db_col in STAT_CLASS_MAP.items():
                        if stat_class in cls:
                            val = parse_int(cell.inner_text(timeout=500))
                            if val > players[name].get(db_col, 0):
                                players[name][db_col] = val
                except:
                    continue

        return len(rows)

    except Exception as e:
        print(f"    Warning scraping {container_id}: {e}")
        return 0


def click_tab(page, side, tab, container_id, max_retries=3):
    """
    Click a tab and VERIFY it actually loaded before returning — this is the
    fix for entire stat categories silently coming back as 0. The old version
    just clicked then slept a fixed 1.5s, so if WhoScored was slow to render
    (common after 30+ back-to-back scrapes / rate limiting), scrape_team_tab
    would read a stale or empty table with no error raised anywhere.
    """
    href = f"#live-player-{side}-{tab}"

    for attempt in range(1, max_retries + 1):
        try:
            page.evaluate(f"document.querySelector(\"a[href='{href}']\").click()")
            # Wait for actual rows to appear in the target container, not just a fixed sleep
            page.wait_for_selector(
                f"#{container_id} #player-table-statistics-body tr",
                timeout=6000
            )
            return True
        except Exception as e:
            if attempt < max_retries:
                print(f"    ⚠️  {side} {tab} tab attempt {attempt}/{max_retries} failed to load, retrying...")
                time.sleep(2)
            else:
                print(f"    ⚠️⚠️  FAILED to load {side} {tab} tab after {max_retries} attempts — "
                      f"stats for this category will be incomplete for this team/match!")
                return False

    return False


def dismiss_overlays(page):
    page.evaluate("""
        const selectors = [
            '.webpush-swal2-container',
            '#adm-sticky-snack-sticky',
            '.gg-overlay-reset',
        ];
        selectors.forEach(sel => {
            const el = document.querySelector(sel);
            if (el) el.remove();
        });
    """)


def accept_cookies(page):
    try:
        for text in ["Accept all", "Accept All", "Accept", "I Accept", "OK"]:
            btn = page.locator(f"button:has-text('{text}')").first
            if btn.is_visible(timeout=2000):
                btn.click()
                print("  Accepted consent dialog.")
                time.sleep(2)
                return
    except:
        pass


def known_gk_names(club):
    """Cross-reference against our own persisted roster data for players
    already tagged GK at this club -- catches in-page detection misses (e.g.
    a substitute keeper brought on whose 'Sub' label never got linked back
    to a GK id, since that link only forms if we'd already seen them start
    as GK earlier in THIS SAME scrape run)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT name FROM players WHERE club = ? AND position = 'GK'", (club,)
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def scrape_saves(page, match_id, home_team, away_team, home_players, away_players):
    try:
        live_url = f"https://www.whoscored.com/matches/{match_id}/live"
        if not goto_with_retry(page, live_url, timeout=30000, retries=2):
            print("  Warning: could not load saves page after retries — saves will be missing")
            return
        time.sleep(3)
        dismiss_overlays(page)

        saves_el = page.locator('[data-for="totalSaves"]').first
        home_saves = parse_int(
            saves_el.locator('[data-field="home"]').first.get_attribute("data-value")
        )
        away_saves = parse_int(
            saves_el.locator('[data-field="away"]').first.get_attribute("data-value")
        )

        def assign_saves(team_players, total_saves, club):
            if total_saves == 0:
                return

            # Union of in-page position detection and our own persisted
            # roster's known GKs at this club -- either signal alone can
            # miss a keeper (e.g. a bench keeper subbed on for the first
            # time this season), the union rarely does.
            gks = {n: p for n, p in team_players.items() if p.get("position", "") == "GK"}
            for name in known_gk_names(club):
                if name in team_players and name not in gks:
                    gks[name] = team_players[name]

            if len(gks) == 1:
                name = list(gks.keys())[0]
                team_players[name]["saves"] = total_saves
                print(f"  Saves: {name} = {total_saves}")
            elif len(gks) >= 2:
                total_mins = sum(p.get("minutes_played", 90) for p in gks.values())
                for name, p in gks.items():
                    mins = p.get("minutes_played", 90)
                    share = round((mins / total_mins) * total_saves) if total_mins > 0 else 0
                    team_players[name]["saves"] = share
                    print(f"  Saves: {name} ({mins} min) = {share}")
            else:
                print(f"  Warning: no GK identified — saves not assigned ({total_saves} total)")

        assign_saves(home_players, home_saves, home_team)
        assign_saves(away_players, away_saves, away_team)

    except Exception as e:
        print(f"  Warning: could not scrape saves: {e}")


def scrape_match(match_id, headless=True):
    """
    Scrape all player stats from a WhoScored match page.
    Returns (players_list, home_team, away_team, goals_home, goals_away).
    """
    url = build_match_url(match_id)
    print(f"Scraping: {url}")

    home_players = {}
    away_players = {}
    home_team = ""
    away_team = ""
    goals_home = 0
    goals_away = 0
    tab_failures = []  # track which tabs failed to load for end-of-run reporting

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda route: route.abort())

            print("  Loading page...")
            if not goto_with_retry(page, url, timeout=60000, retries=2):
                raise Exception(f"Could not load match page after retries: {url}")
            time.sleep(5)

            accept_cookies(page)
            dismiss_overlays(page)

            try:
                home_team = page.locator(".home .team-link").first.inner_text(timeout=5000).strip()
                away_team = page.locator(".away .team-link").first.inner_text(timeout=5000).strip()
                print(f"  Teams: {home_team} vs {away_team}")
            except:
                print("  Warning: could not read team names")

            try:
                score_text = page.locator(".result").first.inner_text(timeout=5000).strip()
                parts = re.split(r'[:\-]', score_text)
                if len(parts) == 2:
                    goals_home = parse_int(parts[0])
                    goals_away = parse_int(parts[1])
                print(f"  Score: {goals_home}-{goals_away}")
            except:
                print("  Warning: could not read score")

            for tab in TABS:
                print(f"  Scraping {tab} tab...")
                dismiss_overlays(page)

                home_container = f"statistics-table-home-{tab}"
                home_ok = click_tab(page, "home", tab, home_container)
                scrape_team_tab(page, home_container, home_team, home_players)
                if not home_ok:
                    tab_failures.append(f"home/{tab}")

                away_container = f"statistics-table-away-{tab}"
                away_ok = click_tab(page, "away", tab, away_container)
                scrape_team_tab(page, away_container, away_team, away_players)
                if not away_ok:
                    tab_failures.append(f"away/{tab}")

            print("  Scraping saves...")
            scrape_saves(page, match_id, home_team, away_team, home_players, away_players)
        finally:
            # Guaranteed even if something above raises — an unclosed browser
            # is an orphaned Chromium process that keeps eating memory long
            # after this match's scrape has moved on or failed.
            browser.close()

    all_players = list(home_players.values()) + list(away_players.values())
    print(f"  Scraped {len(home_players)} home, {len(away_players)} away players.")

    if tab_failures:
        print(f"  ⚠️⚠️  TAB LOAD FAILURES this match: {', '.join(tab_failures)} — "
              f"related stat categories may be incomplete. Consider re-scraping "
              f"this match_id with --rescrape.")

    return all_players, home_team, away_team, goals_home, goals_away


def save_to_db(players, match_id, gw_number, home_team, away_team,
               goals_home, goals_away, rescrape=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if rescrape:
        deleted = c.execute(
            "DELETE FROM raw_stats WHERE match_id = ?", (match_id,)
        ).rowcount
        print(f"  Re-scrape: deleted {deleted} rows for match {match_id}.")

    gw_row = c.execute("SELECT id FROM gameweeks WHERE gw_number=?", (gw_number,)).fetchone()
    if not gw_row:
        c.execute("INSERT INTO gameweeks (gw_number, season, status) VALUES (?, '2025-26', 'complete')",
                  (gw_number,))
        conn.commit()
        gw_row = c.execute("SELECT id FROM gameweeks WHERE gw_number=?", (gw_number,)).fetchone()
    gw_id = gw_row[0]

    if c.execute("SELECT id FROM fixtures WHERE match_id=?", (match_id,)).fetchone():
        c.execute("UPDATE fixtures SET goals_home=?, goals_away=? WHERE match_id=?",
                  (goals_home, goals_away, match_id))
    else:
        c.execute("INSERT INTO fixtures (gw_id, match_id, home_club, away_club, goals_home, goals_away) VALUES (?,?,?,?,?,?)",
                  (gw_id, match_id, home_team, away_team, goals_home, goals_away))

    inserted = 0
    for p in players:
        if not rescrape:
            if c.execute("SELECT 1 FROM raw_stats WHERE player_name=? AND match_id=?",
                         (p["player_name"], match_id)).fetchone():
                continue
        c.execute("""
            INSERT INTO raw_stats (
                match_id, player_name, club, gw_number,
                goals, assists, pk_saves, yellow_cards, red_cards,
                glc, lmt, elg, own_goals, motm,
                sub_on_min, sub_off_min,
                shots_on_target, key_passes, dribbles, tackles,
                interceptions, clearances, blocked_shots, saves,
                acc_crosses, acc_long_balls, minutes_played
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            match_id, p["player_name"], p["club"], gw_number,
            p["goals"], p["assists"], p["pk_saves"],
            p["yellow_cards"], p["red_cards"],
            p["glc"], p["lmt"], p["elg"], p["own_goals"], p["motm"],
            p["sub_on_min"], p["sub_off_min"],
            p["shots_on_target"], p["key_passes"], p["dribbles"], p["tackles"],
            p["interceptions"], p["clearances"], p["blocked_shots"], p["saves"],
            p["acc_crosses"], p["acc_long_balls"], p["minutes_played"]
        ))
        # Persist WhoScored player ID for future position-eligibility re-scrapes
        if p.get("player_id"):
            c.execute(
                "UPDATE players SET whoscored_id=? WHERE name=? AND whoscored_id IS NULL",
                (p["player_id"], p["player_name"])
            )
        inserted += 1

    conn.commit()
    conn.close()
    print(f"  Saved {inserted} rows for match {match_id} (GW{gw_number}).")


MIN_EXPECTED_PLAYERS = 10  # two full squads is ~30-36; anything under this means the scrape effectively failed


def compare_with_db(players, match_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    stat_cols = [
        "goals", "assists", "shots_on_target", "key_passes", "dribbles",
        "tackles", "interceptions", "clearances", "blocked_shots", "saves",
        "acc_crosses", "acc_long_balls", "own_goals", "yellow_cards",
        "red_cards", "motm", "pk_saves", "glc", "lmt", "elg",
        "sub_on_min", "sub_off_min", "minutes_played"
    ]

    print(f"\n── Comparing scraped vs DB for match {match_id} ──")

    # A "perfect match" against the DB is meaningless if we captured almost
    # no players — that usually means the match hasn't been played yet or
    # the page structure changed, not that the scrape succeeded. Without
    # this check, 0 scraped players compared to 0 DB rows reports as a
    # false "Perfect match!" even though nothing was actually captured.
    if len(players) < MIN_EXPECTED_PLAYERS:
        print(f"  ❌ SCRAPE FAILED — only {len(players)} player(s) captured (expected 20+ for two full squads).")
        print(f"     Likely cause: this match hasn't been played yet, or WhoScored's page structure changed.")
        conn.close()
        return None  # sentinel distinct from 0 (perfect) or >0 (discrepancies)

    diffs = 0
    scraped_by_name = {p["player_name"]: p for p in players}
    db_rows = c.execute(
        f"SELECT player_name, {', '.join(stat_cols)} FROM raw_stats WHERE match_id=?",
        (match_id,)
    ).fetchall()

    for row in db_rows:
        name = row[0]
        db_stats = dict(zip(stat_cols, row[1:]))
        scraped = scraped_by_name.get(name)
        if not scraped:
            print(f"  {name}: in DB but NOT scraped")
            diffs += 1
            continue
        for col in stat_cols:
            db_val = db_stats.get(col) or 0
            sc_val = scraped.get(col) or 0
            if db_val != sc_val:
                print(f"  {name} | {col}: DB={db_val} scraped={sc_val}")
                diffs += 1

    for name in scraped_by_name:
        if not any(r[0] == name for r in db_rows):
            print(f"  {name}: scraped but NOT in DB")
            diffs += 1

    if diffs == 0:
        print("  ✅ Perfect match!")
    else:
        print(f"  ⚠️  {diffs} discrepancies found.")

    conn.close()
    return diffs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--match_id", type=int, required=True)
    parser.add_argument("--gw",       type=int, required=True)
    parser.add_argument("--rescrape", action="store_true")
    parser.add_argument("--compare",  action="store_true")
    parser.add_argument("--visible",  action="store_true")
    args = parser.parse_args()

    players, home_team, away_team, goals_home, goals_away = scrape_match(
        args.match_id, headless=not args.visible
    )

    if len(players) < MIN_EXPECTED_PLAYERS:
        # Don't persist a failed scrape — saving here would overwrite real
        # data with a bogus 0-0 score and empty stat rows.
        compare_with_db(players, args.match_id)
        sys.exit(1)

    if args.compare:
        diffs = compare_with_db(players, args.match_id)
    else:
        save_to_db(players, args.match_id, args.gw,
                   home_team, away_team, goals_home, goals_away,
                   rescrape=args.rescrape)
        diffs = compare_with_db(players, args.match_id)

    sys.exit(0 if diffs == 0 else 1)
