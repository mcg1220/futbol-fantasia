"""
One-off: add Gerónimo Rulli (Manchester City backup GK) to the player pool.

He showed up as an UNMATCHED orphan in fix_accent_mismatches.py -- scraped
this season under his real (accented) name, but never added to `players`
under any spelling at all. Adding him directly under the accented spelling
avoids ever hitting the same un-accented-canonical bug this player pool
has repeatedly had this week.

    python3 add_geronimo_rulli.py            # dry run
    python3 add_geronimo_rulli.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

NAME = "Gerónimo Rulli"
CLUB = "Manchester City"
POSITION = "GK"
SOURCE = "manual_add"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    existing = conn.execute("SELECT id FROM players WHERE name=?", (NAME,)).fetchone()
    if existing:
        print(f"{NAME!r} already exists (id={existing['id']}) — nothing to do.")
        return

    print(f"Will add: name={NAME!r}, club={CLUB!r}, position={POSITION!r}, "
          f"plus a player_eligibility row for {POSITION!r} (source={SOURCE!r}).")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute(
        "INSERT INTO players (name, club, position, draftable) VALUES (?, ?, ?, 1)",
        (NAME, CLUB, POSITION)
    )
    player_id = conn.execute("SELECT id FROM players WHERE name=?", (NAME,)).fetchone()[0]
    conn.execute(
        "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
        (player_id, POSITION, SOURCE)
    )
    conn.commit()
    print(f"\nApplied — added {NAME!r} (id={player_id}).")
    conn.close()


if __name__ == '__main__':
    main()
