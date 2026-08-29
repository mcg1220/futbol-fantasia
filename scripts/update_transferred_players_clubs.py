"""
One-off: batch club update for players who transferred clubs, found by
diffing a Fantrax player export against our pool (2026-08-29). Includes
players who previously had no club at all -- confirmed as genuine new
arrivals into the Premier League, not stale data.

    python3 update_transferred_players_clubs.py            # dry run
    python3 update_transferred_players_clubs.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

# (player name as it appears in players.name, new club)
TRANSFERS = [
    ("Savio", "Tottenham"),
    ("Omar Marmoush", "Tottenham"),
    ("Nico González", "Newcastle"),
    ("Carlos Baleba", "Manchester United"),
    ("Liam Delap", "Nottingham Forest"),
    ("Axel Disasi", "Crystal Palace"),
    ("Evann Guessand", "Crystal Palace"),
    ("Aaron Wan-Bissaka", "Aston Villa"),
    ("El Hadji Malick Diouf", "Brentford"),
    ("Taiwo Awoniyi", "Coventry"),
    ("Ethan Pinnock", "Coventry"),
    ("Joe Gelhardt", "Hull"),
    ("Julio Enciso", "Ipswich"),
    ("Gustavo Hamer", "Coventry"),
    ("Nicolas Jackson", "Aston Villa"),
    ("Steven Benda", "Nottingham Forest"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    plan = []
    for name, new_club in TRANSFERS:
        row = conn.execute("SELECT id, club FROM players WHERE name=?", (name,)).fetchone()
        if not row:
            print(f"SKIP — no player named {name!r} found.")
            continue
        old_club = row['club'] or '(none)'
        if row['club'] == new_club:
            print(f"SKIP — {name!r} is already at {new_club!r}.")
            continue
        plan.append((name, old_club, new_club))
        print(f"{name}: {old_club} -> {new_club}")

    if not plan:
        print("\nNothing to do.")
        return

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write these {len(plan)} change(s).")
        return

    for name, _, new_club in plan:
        conn.execute("UPDATE players SET club=? WHERE name=?", (new_club, name))
    conn.commit()
    print(f"\nApplied — {len(plan)} player(s) updated.")
    conn.close()


if __name__ == '__main__':
    main()
