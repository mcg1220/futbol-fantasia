"""
One-off: fix Sávio's missing stats after his Manchester City -> Tottenham
transfer.

Confirmed via https://www.whoscored.com/players/397821/show/s%C3%A1vio :
real name "Sávio" (full name Sávio Moreira de Oliveira), whoscored_id
397821, current club Tottenham, position Attacking Midfielder (-> MID).

This inspects the actual live data before deciding what to do, rather
than assuming one specific bug shape, because there are two different
possible causes with two different fixes:

  1. players.club is just stale (still 'Manchester City') -- a plain
     club update fixes it, same as the other one-off club scripts this
     season.
  2. There are two separate `players` rows for him (one under each
     spelling, "Savio" vs "Sávio") -- the same duplicate-player bug
     found with Andy/Andrew Robertson, where a still-active old row
     shadows a newer correctly-spelled one (or vice versa). This needs
     a merge, not just a club update.

The tie-breaker for which spelling should survive a merge is whichever
one his real raw_stats rows are actually scraped under this season (if
any exist yet) -- that's what scoring depends on, and it may not
perfectly match WhoScored's own profile-page rendering. If he has no
raw_stats appearances yet this season under either spelling, this
defaults to "Sávio" (WhoScored's own spelling) as the safer bet for
future scrapes to land on correctly.

Only ever writes players.club/name and repoints other tables' player-name
text columns (never touches raw_stats, which is scraper ground truth).

Run: python3 fix_savio.py            # reports findings, no writes
     python3 fix_savio.py --apply    # applies the fix it determined
"""
import argparse
import sqlite3
import unicodedata

from init_db import DB_PATH

SEASON = '2026-27'
WHOSCORED_NAME = 'Sávio'
WHOSCORED_ID = 397821
CORRECT_CLUB = 'Tottenham'
CORRECT_POSITION = 'MID'

NAME_TEXT_COLUMNS = [
    ('player_trade_items', ['player_name']),
    ('rosters', ['player_name']),
    ('draft_picks', ['player_name']),
    ('shortlists', ['player_name']),
    ('transactions', ['added_player', 'dropped_player']),
    ('transfer_draft_picks', ['player_name', 'dropped_player']),
    ('pending_waiver_claims', ['add_player', 'drop_player']),
    ('transfer_pool', ['player_name']),
    ('waiver_claims', ['add_player', 'drop_player']),
    ('player_projections', ['player_name']),
    ('player_start_status', ['player_name']),
]


def normalize(name):
    d = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().strip()


def rename_everywhere(conn, old_name, new_name):
    for table, columns in NAME_TEXT_COLUMNS:
        for col in columns:
            cur = conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new_name, old_name))
            if cur.rowcount:
                print(f"  {table}.{col}: renamed {cur.rowcount} row(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='apply the determined fix (default: report only)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    player_rows = [r for r in c.execute("SELECT * FROM players").fetchall()
                   if normalize(r['name']) == normalize(WHOSCORED_NAME)]

    raw_appearances = c.execute("""
        SELECT DISTINCT rs.player_name, rs.club, g.gw_number
        FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season=? AND rs.external=0
    """, (SEASON,)).fetchall()
    matching_raw = [r for r in raw_appearances if normalize(r['player_name']) == normalize(WHOSCORED_NAME)]

    print(f"players rows matching {WHOSCORED_NAME!r} (any spelling): {len(player_rows)}")
    for r in player_rows:
        print(f"  #{r['id']} {r['name']!r} -- club={r['club']!r} position={r['position']!r} "
              f"whoscored_id={r['whoscored_id']} draftable={r['draftable']}")
    print(f"\nraw_stats appearances matching {WHOSCORED_NAME!r} this season: {len(matching_raw)}")
    for r in matching_raw:
        print(f"  GW{r['gw_number']}: {r['player_name']!r} for {r['club']!r}")

    if not player_rows:
        print(f"\nNo players row found for {WHOSCORED_NAME!r} at all -- not something this script handles "
              f"(that would be a missing player, not a stale/duplicate one). Aborting.")
        conn.close()
        return

    # Ground truth for the surviving spelling: whatever raw_stats actually
    # used, if he's played a scraped match yet; otherwise WhoScored's own
    # spelling, since that's the best bet for future scrapes to match.
    final_name = matching_raw[0]['player_name'] if matching_raw else WHOSCORED_NAME

    if len(player_rows) == 1:
        row = player_rows[0]
        needs_name_fix = row['name'] != final_name
        needs_club_fix = row['club'] != CORRECT_CLUB
        if not needs_name_fix and not needs_club_fix:
            print(f"\nSingle row already correct (name={row['name']!r}, club={row['club']!r}). Nothing to do.")
            conn.close()
            return
        print(f"\nPlan: single row #{row['id']} -- "
              f"{'rename to ' + repr(final_name) + ' ' if needs_name_fix else ''}"
              f"{'club -> ' + repr(CORRECT_CLUB) if needs_club_fix else ''}")
        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            conn.close()
            return
        if needs_name_fix:
            rename_everywhere(conn, row['name'], final_name)
            conn.execute("UPDATE players SET name=? WHERE id=?", (final_name, row['id']))
        conn.execute(
            "UPDATE players SET club=?, position=?, whoscored_id=COALESCE(whoscored_id, ?), draftable=1 WHERE id=?",
            (CORRECT_CLUB, CORRECT_POSITION, WHOSCORED_ID, row['id'])
        )
        conn.commit()
        print("Applied.")
        conn.close()
        return

    # Duplicate rows -- merge into the one matching final_name (creating it
    # as the keeper), same pattern as fix_robertson_duplicate_and_add_mendy.py.
    keep = next((r for r in player_rows if r['name'] == final_name), None)
    if not keep:
        keep = max(player_rows, key=lambda r: r['id'])  # newest row as a fallback keeper
    others = [r for r in player_rows if r['id'] != keep['id']]

    print(f"\nPlan: merge {len(others)} duplicate row(s) into #{keep['id']} ({final_name!r}), "
          f"club={CORRECT_CLUB!r}")
    for o in others:
        print(f"  merging #{o['id']} {o['name']!r} -> #{keep['id']}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        conn.close()
        return

    if keep['name'] != final_name:
        rename_everywhere(conn, keep['name'], final_name)
        conn.execute("UPDATE players SET name=? WHERE id=?", (final_name, keep['id']))

    conn.execute(
        "UPDATE players SET club=?, position=?, whoscored_id=COALESCE(whoscored_id, ?), draftable=1 WHERE id=?",
        (CORRECT_CLUB, CORRECT_POSITION, WHOSCORED_ID, keep['id'])
    )

    for o in others:
        elig = c.execute("SELECT position, source FROM player_eligibility WHERE player_id=?", (o['id'],)).fetchall()
        for pos, source in elig:
            conn.execute(
                "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                (keep['id'], pos, source)
            )
        conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (o['id'],))
        if o['name'] != final_name:
            rename_everywhere(conn, o['name'], final_name)
        conn.execute("DELETE FROM players WHERE id=?", (o['id'],))

    conn.commit()
    print("Applied.")
    conn.close()


if __name__ == '__main__':
    main()
