"""
One-off: rename Jeremy Jacquet's canonical spelling to match WhoScored.

His player record everywhere in the app (players, rosters, draft_picks,
etc.) uses "Jeremy Jacquet" (no accent), but WhoScored's own match pages
render his name as "Jérémy Jacquet" -- and scraper.py takes whatever text
WhoScored shows verbatim, with no accent-normalization. That mismatch made
his GW1 Newcastle vs Liverpool stats (scraped correctly, under the accented
spelling) invisible to his fantasy team, since nothing else in the app
used that spelling.

Renaming the canonical record to match WhoScored (rather than renaming the
scraped row to match the canonical record) fixes this permanently -- every
future scrape of this player will already match. This updates every table
that references a player by name string, including raw_stats itself (his
older, backfilled Rennes rows are still under the un-accented spelling and
need to move too, or *they'd* become the orphaned ones instead).

    python3 fix_jacquet_name.py            # dry run
    python3 fix_jacquet_name.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

WRONG_NAME = "Jeremy Jacquet"
CANONICAL_NAME = "Jérémy Jacquet"

# (table, column) pairs -- every place a player is referenced by name string.
TARGETS = [
    ("players", "name"),
    ("rosters", "player_name"),
    ("draft_picks", "player_name"),
    ("raw_stats", "player_name"),
    ("transfer_pool", "player_name"),
    ("transfer_draft_picks", "player_name"),
    ("transfer_draft_picks", "dropped_player"),
    ("shortlists", "player_name"),
    ("player_trade_items", "player_name"),
    ("player_projections", "player_name"),
    ("transactions", "added_player"),
    ("transactions", "dropped_player"),
    ("waiver_claims", "add_player"),
    ("waiver_claims", "drop_player"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = 0
    plan = []
    for table, col in TARGETS:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (WRONG_NAME,)).fetchone()[0]
        if count:
            plan.append((table, col, count))
            total += count

    if not total:
        print(f"No rows found for {WRONG_NAME!r} anywhere — nothing to do.")
        return

    print(f"Rows to rename from {WRONG_NAME!r} to {CANONICAL_NAME!r}:")
    for table, col, count in plan:
        print(f"  {table}.{col}: {count} row(s)")

    colliding = conn.execute("SELECT COUNT(*) FROM players WHERE name=?", (CANONICAL_NAME,)).fetchone()[0]
    if colliding:
        print(f"\nABORT: players.name already has a row for {CANONICAL_NAME!r} — "
              "renaming would collide. Investigate before proceeding.")
        return

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write.")
        return

    for table, col, _ in plan:
        conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (CANONICAL_NAME, WRONG_NAME))
    conn.commit()
    print(f"\nApplied — renamed {total} row(s) across {len(plan)} table(s) to {CANONICAL_NAME!r}.")
    conn.close()


if __name__ == '__main__':
    main()
