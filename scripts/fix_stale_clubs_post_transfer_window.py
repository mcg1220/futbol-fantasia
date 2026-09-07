"""
One-off: fix players.club for players whose club went stale after the
summer transfer window, found via scripts/check_stale_player_clubs.py and
cross-referenced against a Fantrax export (matched by club + position,
never by name spelling -- Fantrax drops accents and one entry uses a
different first name, so name text alone isn't a reliable match key here).

Does NOT touch any player's name -- only the club field. WhoScored's
spelling of a name is the source of truth for this app; nothing here
changes that.

Tim Iroegbunam was investigated and deliberately excluded: his
players.club ('Hull') is already correct (Fantrax agrees) -- his one
raw_stats appearance for Everton is historical, from before his transfer
completed, not evidence of a stale club.

    python3 fix_stale_clubs_post_transfer_window.py            # dry run
    python3 fix_stale_clubs_post_transfer_window.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

FIXES = [
    ('Ben Chilwell', 'Crystal Palace'),
    ('Dan Bentley', 'Coventry'),
    ('Daniel Muñoz', 'Nottingham Forest'),
    ('Enzo Fernández', 'Manchester City'),
    ('Lewis Koumas', 'Liverpool'),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    to_apply = []
    for name, new_club in FIXES:
        row = conn.execute("SELECT id, name, club, position FROM players WHERE name=?", (name,)).fetchone()
        if not row:
            print(f"No player named {name!r} found -- skipping.")
            continue
        if row['club'] == new_club:
            print(f"{name} already shows {new_club!r} -- nothing to do.")
            continue
        print(f"Before: #{row['id']} {row['name']} — {row['club']!r} ({row['position']})")
        print(f"After:  #{row['id']} {row['name']} — {new_club!r} ({row['position']})")
        to_apply.append((row['id'], name, new_club))

    if not to_apply:
        print("\nNothing to change.")
        conn.close()
        return

    if not args.apply:
        print(f"\nDry run -- {len(to_apply)} player(s) would be updated. Re-run with --apply to write.")
        conn.close()
        return

    for player_id, name, new_club in to_apply:
        conn.execute("UPDATE players SET club=? WHERE id=?", (new_club, player_id))
    conn.commit()
    print(f"\nApplied. {len(to_apply)} player(s) updated.")
    conn.close()


if __name__ == '__main__':
    main()
