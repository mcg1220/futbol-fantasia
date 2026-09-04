"""
One-off: Jack Grealish transfered from Manchester City to Everton.

    python3 fix_grealish_club.py            # dry run
    python3 fix_grealish_club.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

PLAYER_NAME = 'Jack Grealish'
NEW_CLUB = 'Everton'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute("SELECT id, name, club, position FROM players WHERE name=?", (PLAYER_NAME,)).fetchone()
    if not row:
        print(f"No player named {PLAYER_NAME!r} found -- nothing to do.")
        return

    print(f"Before: #{row['id']} {row['name']} — {row['club']} ({row['position']})")
    print(f"After:  #{row['id']} {row['name']} — {NEW_CLUB} ({row['position']})")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute("UPDATE players SET club=? WHERE name=?", (NEW_CLUB, PLAYER_NAME))
    conn.commit()
    print("\nApplied.")
    conn.close()


if __name__ == '__main__':
    main()
