"""
One-off fix for two player-identity gaps found via user report + WhoScored:

1. DUPLICATE PLAYER: "Andrew Robertson" (Tottenham) and "Andy Robertson"
   (Liverpool, stale) are two separate rows in `players` for the same real
   person -- confirmed via https://www.whoscored.com/players/115726/show/
   andy-robertson, whoscored_id=115726, real name "Andy Robertson". The
   duplicate ("Andrew Robertson") was almost certainly created during this
   summer's Fantrax CSV club-transfer reconciliation, which matched Fantrax's
   full-first-name spelling instead of the existing WhoScored-sourced row.
   All of his real raw_stats (38+ rows) are correctly scraped under "Andy
   Robertson" -- the OLD row -- so the "Andrew Robertson" row (currently the
   draftable/current one, showing 0.00 everywhere) is the one silently
   orphaned from his actual stats. This merges them: "Andy Robertson"
   survives (matches raw_stats and WhoScored, so scoring keeps working with
   zero further action), taking the current club/draftable state from
   "Andrew Robertson", and every other table referencing "Andrew Robertson"
   by name gets repointed. Skips raw_stats on purpose -- it already has the
   correct spelling and needs no change.

   This is a general pattern (a name-spelling duplicate created by an
   import), not just a one-off for Robertson -- if this recurs, adapt the
   KEEP_NAME/REMOVE_NAME/final club-position-draftable block below.

2. MISSING PLAYER: "Nobel Mendy" (Hull, DEF) doesn't exist in `players` at
   all yet, confirmed via https://www.whoscored.com/players/469508/show/
   nobel-mendy -- he's been scoring real minutes there with no player row to
   attach to. Added the same way "+ Add Brand-New Player" would.

Run once: python3 fix_robertson_duplicate_and_add_mendy.py
"""
import sqlite3
from datetime import datetime

from init_db import DB_PATH

# ── Part 1: merge the Robertson duplicate ───────────────────────────────
KEEP_NAME = 'Andy Robertson'      # matches raw_stats + WhoScored's real name
REMOVE_NAME = 'Andrew Robertson'  # the duplicate created by CSV import
FINAL_CLUB = 'Tottenham'
FINAL_POSITION = 'DEF'
FINAL_WHOSCORED_ID = 115726
FINAL_DRAFTABLE = 1

# Every table/column (besides raw_stats, deliberately excluded -- see
# docstring) that stores a player's name as free text rather than a
# player_id foreign key.
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


def merge_robertson(conn):
    c = conn.cursor()
    keep = c.execute("SELECT * FROM players WHERE name=?", (KEEP_NAME,)).fetchone()
    remove = c.execute("SELECT * FROM players WHERE name=?", (REMOVE_NAME,)).fetchone()

    if not remove:
        print(f"No '{REMOVE_NAME}' row found -- already merged, or never existed here. Skipping.")
        return
    if not keep:
        print(f"No '{KEEP_NAME}' row found to merge into -- nothing to do. Skipping.")
        return

    keep_id, remove_id = keep['id'], remove['id']
    print(f"Merging players.id={remove_id} ('{REMOVE_NAME}') into id={keep_id} ('{KEEP_NAME}')")

    c.execute(
        "UPDATE players SET club=?, position=?, whoscored_id=?, draftable=? WHERE id=?",
        (FINAL_CLUB, FINAL_POSITION, FINAL_WHOSCORED_ID, FINAL_DRAFTABLE, keep_id)
    )

    remove_elig = c.execute("SELECT position, source FROM player_eligibility WHERE player_id=?", (remove_id,)).fetchall()
    for pos, source in remove_elig:
        c.execute(
            "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
            (keep_id, pos, source)
        )
    c.execute("DELETE FROM player_eligibility WHERE player_id=?", (remove_id,))

    for table, columns in NAME_TEXT_COLUMNS:
        for col in columns:
            cur = conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (KEEP_NAME, REMOVE_NAME))
            if cur.rowcount:
                print(f"  {table}.{col}: renamed {cur.rowcount} row(s)")

    c.execute("DELETE FROM players WHERE id=?", (remove_id,))
    print(f"Merge complete. '{REMOVE_NAME}' (id={remove_id}) removed; '{KEEP_NAME}' (id={keep_id}) is now "
          f"club={FINAL_CLUB}, position={FINAL_POSITION}, whoscored_id={FINAL_WHOSCORED_ID}, draftable={FINAL_DRAFTABLE}.")


# ── Part 2: add the missing player ──────────────────────────────────────
def add_mendy(conn):
    c = conn.cursor()
    existing = c.execute("SELECT id FROM players WHERE name='Nobel Mendy'").fetchone()
    if existing:
        print("'Nobel Mendy' already exists -- skipping add.")
        return

    c.execute(
        "INSERT INTO players (name, club, position, whoscored_id, draftable) VALUES (?,?,?,?,1)",
        ('Nobel Mendy', 'Hull', 'DEF', 469508)
    )
    player_id = c.execute("SELECT id FROM players WHERE name='Nobel Mendy'").fetchone()[0]
    c.execute(
        "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
        (player_id, 'DEF', 'manual')
    )
    print(f"Added 'Nobel Mendy' (Hull, DEF, whoscored_id=469508) as players.id={player_id}.")


if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        merge_robertson(conn)
        add_mendy(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
