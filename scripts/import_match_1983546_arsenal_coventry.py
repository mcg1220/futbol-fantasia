"""
One-off: import Arsenal vs Coventry (match_id 1983546, GW1)
raw_stats + final score, scraped locally from a residential IP as a
holdover while we get a residential proxy wired up for Render (see
scripts/diagnose_scrape.py -- WhoScored's Cloudflare protection is
currently hard-blocking Render's server IP).

Idempotent: skips any player row that already exists for this match_id,
same convention as scraper.py's own non-rescrape save path. Safe to run
more than once.

    python3 import_match_1983546_arsenal_coventry.py            # dry run
    python3 import_match_1983546_arsenal_coventry.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

MATCH_ID = 1983546
GOALS_HOME = 3
GOALS_AWAY = 0

COLUMNS = ['match_id', 'player_name', 'club', 'gw_number', 'goals', 'assists', 'pk_saves', 'yellow_cards', 'red_cards', 'glc', 'lmt', 'elg', 'own_goals', 'motm', 'sub_off_min', 'sub_on_min', 'shots_on_target', 'key_passes', 'dribbles', 'tackles', 'interceptions', 'clearances', 'blocked_shots', 'saves', 'acc_crosses', 'acc_long_balls', 'minutes_played', 'external']

ROWS = [(1983546, 'David Raya', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 5, 90, 0), (1983546, 'Ben White', 'Arsenal', 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 1, 1, 3, 0, 0, 0, 2, 90, 0), (1983546, 'Gabriel Magalhães', 'Arsenal', 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 3, 0, 0, 0, 1, 90, 0), (1983546, 'Cristhian Mosquera', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 5, 0, 0, 0, 0, 90, 0), (1983546, 'Riccardo Calafiori', 'Arsenal', 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 80, 0, 0, 1, 0, 1, 0, 4, 0, 0, 0, 0, 80, 0), (1983546, 'Declan Rice', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 67, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 6, 67, 0), (1983546, 'Myles Lewis-Skelly', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 0, 3, 90, 0), (1983546, 'Bukayo Saka', 'Arsenal', 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 67, 0, 1, 2, 1, 1, 0, 2, 0, 0, 0, 0, 67, 0), (1983546, 'Martin Ødegaard', 'Arsenal', 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 75, 0, 1, 3, 0, 3, 0, 1, 0, 0, 0, 2, 75, 0), (1983546, 'Christos Tzolis', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 75, 0, 2, 3, 0, 1, 0, 2, 0, 0, 1, 1, 75, 0), (1983546, 'Kai Havertz', 'Arsenal', 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 90, 0), (1983546, 'Piero Hincapié', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10, 0), (1983546, 'Mikel Merino', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 75, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 15, 0), (1983546, 'Martín Zubimendi', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 67, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 23, 0), (1983546, 'Eberechi Eze', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 75, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 15, 0), (1983546, 'Noni Madueke', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 67, 0, 4, 1, 0, 0, 0, 0, 0, 0, 0, 23, 0), (1983546, 'Kepa Arrizabalaga', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Viktor Gyökeres', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Ethan Nwaneri', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Max Dowman', 'Arsenal', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Carl Rushworth', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 18, 90, 0), (1983546, 'Milan van Ewijk', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 3, 2, 0, 0, 0, 90, 0), (1983546, 'Aurèle Amenda', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 3, 0, 0, 8, 90, 0), (1983546, 'Bobby Thomas', 'Coventry', 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 7, 2, 0, 0, 0, 90, 0), (1983546, 'Jay Dasilva', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 5, 1, 0, 0, 1, 90, 0), (1983546, 'Matt Grimes', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 1, 0, 0, 0, 90, 0), (1983546, 'Loum Tchaouna', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 81, 0, 0, 0, 1, 0, 0, 3, 0, 0, 0, 0, 81, 0), (1983546, 'Caleb Yirenkyi', 'Coventry', 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 61, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 61, 0), (1983546, 'Frank Onyeka', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 3, 0, 0, 0, 0, 4, 90, 0), (1983546, 'Brandon Thomas-Asante', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 69, 0, 0, 0, 0, 0, 4, 1, 0, 0, 0, 0, 69, 0), (1983546, 'Ellis Simms', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 69, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 69, 0), (1983546, 'Victor Torp', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 61, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 29, 0), (1983546, 'Gustavo Hamer', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 81, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9, 0), (1983546, 'Taiwo Awoniyi', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 69, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 21, 0), (1983546, 'Jack Rudoni', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 69, 1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 21, 0), (1983546, 'Joel Latibeaudiere', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Tatsuhiro Sakamoto', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Ben Wilson', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 90, 0), (1983546, 'Liam Kitching', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0), (1983546, 'Josh Eccles', 'Coventry', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 0)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    fx = conn.execute("SELECT match_id, goals_home, goals_away FROM fixtures WHERE match_id=?", (MATCH_ID,)).fetchone()
    if not fx:
        print(f"No fixtures row for match_id={MATCH_ID} -- aborting, DB may not be seeded for this gw.")
        return
    print(f"fixtures row before: goals_home={fx['goals_home']} goals_away={fx['goals_away']}")
    print(f"fixtures row after:  goals_home={GOALS_HOME} goals_away={GOALS_AWAY}")

    existing = {r[0] for r in conn.execute("SELECT player_name FROM raw_stats WHERE match_id=?", (MATCH_ID,))}
    to_insert = [r for r in ROWS if r[COLUMNS.index('player_name')] not in existing]
    print(f"\n{len(ROWS)} scraped player rows, {len(existing)} already in DB, {len(to_insert)} to insert.")
    for r in to_insert:
        print(f"  + {r[COLUMNS.index('player_name')]}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute("UPDATE fixtures SET goals_home=?, goals_away=? WHERE match_id=?", (GOALS_HOME, GOALS_AWAY, MATCH_ID))
    placeholders = ','.join('?' * len(COLUMNS))
    conn.executemany(
        f"INSERT INTO raw_stats ({', '.join(COLUMNS)}) VALUES ({placeholders})",
        to_insert
    )
    conn.commit()
    print(f"\nApplied -- inserted {len(to_insert)} raw_stats rows, updated fixtures score to {GOALS_HOME}-{GOALS_AWAY}.")
    conn.close()


if __name__ == '__main__':
    main()
