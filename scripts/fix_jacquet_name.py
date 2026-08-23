"""
One-off: merge Jeremy Jacquet's GW1 stats into his canonical player_name.

His GW1 Newcastle vs Liverpool match (match_id 1983547) got scraped and
saved correctly, but under "Jérémy Jacquet" (the exact accented spelling
WhoScored renders) -- a name that doesn't match the "Jeremy Jacquet" (no
accent) spelling used everywhere else in the app (players table, rosters,
draft board), so his real stats were invisible to his fantasy team. This
renames that orphaned raw_stats row to the canonical spelling.

    python3 fix_jacquet_name.py            # dry run
    python3 fix_jacquet_name.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

WRONG_NAME = "Jérémy Jacquet"
CANONICAL_NAME = "Jeremy Jacquet"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, match_id, gw_number, club, minutes_played FROM raw_stats WHERE player_name=?",
        (WRONG_NAME,)
    ).fetchall()

    if not rows:
        print(f"No raw_stats rows found for {WRONG_NAME!r} — nothing to do.")
        return

    print(f"raw_stats rows for {WRONG_NAME!r} ({len(rows)}):")
    for r in rows:
        print(f"  #{r['id']} match={r['match_id']} gw={r['gw_number']} club={r['club']} mins={r['minutes_played']}")

    existing = conn.execute(
        "SELECT match_id FROM raw_stats WHERE player_name=?", (CANONICAL_NAME,)
    ).fetchall()
    colliding = {r['match_id'] for r in existing} & {r['match_id'] for r in rows}
    if colliding:
        print(f"\nABORT: {CANONICAL_NAME!r} already has row(s) for match_id(s) {colliding} — "
              "renaming would collide. Investigate before proceeding.")
        return

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to rename to {CANONICAL_NAME!r}.")
        return

    conn.execute("UPDATE raw_stats SET player_name=? WHERE player_name=?", (CANONICAL_NAME, WRONG_NAME))
    conn.commit()
    print(f"\nApplied — renamed {len(rows)} row(s) from {WRONG_NAME!r} to {CANONICAL_NAME!r}.")
    conn.close()


if __name__ == '__main__':
    main()
