"""
One-off: undo Joe's 2026-08-24 12:59 add of Kevin Schade (roster_swap into an
open roster spot) at the user's explicit request, overriding normal lock
rules (this is a direct DB fix, not the app's swap endpoint, so
is_change_locked is never consulted).

Reverses it completely: deletes the roster row Schade's add created (so no
ghost row remains claiming he was ever rostered) and deletes the matching
transactions row, freeing the open roster spot back up. Logs one audit_log
entry noting the manual override so there's a paper trail.

    python3 undo_schade_transaction.py            # dry run
    python3 undo_schade_transaction.py --apply
"""
import argparse
import json
import sqlite3
from datetime import datetime

from init_db import DB_PATH

MANAGER_NAME = 'Joe'
PLAYER_NAME = 'Kevin Schade'
TXN_CREATED_AT_PREFIX = '2026-08-24T12:59'
SOURCE = 'roster_swap'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    manager = conn.execute("SELECT id, name, team_name FROM managers WHERE name=?", (MANAGER_NAME,)).fetchone()
    if not manager:
        print(f"No manager named {MANAGER_NAME!r} found -- nothing to do.")
        return
    manager_id = manager['id']
    print(f"Manager: #{manager_id} {manager['name']} — {manager['team_name']}")

    txn = conn.execute("""
        SELECT id, manager_id, added_player, dropped_player, source, gw, season, created_at
        FROM transactions
        WHERE manager_id=? AND added_player=? AND source=? AND created_at LIKE ?
    """, (manager_id, PLAYER_NAME, SOURCE, TXN_CREATED_AT_PREFIX + '%')).fetchone()
    if not txn:
        print(f"No matching transaction found for manager_id={manager_id}, added_player={PLAYER_NAME!r}, "
              f"source={SOURCE!r}, created_at like {TXN_CREATED_AT_PREFIX!r} -- nothing to do.")
        return
    print(f"Transaction: id={txn['id']} gw={txn['gw']} season={txn['season']} created_at={txn['created_at']}")

    roster_row = conn.execute("""
        SELECT id, player_name, slot_type, position_slot, gw_start, gw_end
        FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, PLAYER_NAME, txn['gw'], txn['gw'])).fetchone()
    if not roster_row:
        print(f"No active roster row found for {PLAYER_NAME} on manager_id={manager_id} at gw={txn['gw']} "
              f"-- transaction row exists but roster row doesn't (already reverted?). Aborting.")
        return
    print(f"Roster row: id={roster_row['id']} slot_type={roster_row['slot_type']} "
          f"position_slot={roster_row['position_slot']} gw_start={roster_row['gw_start']} gw_end={roster_row['gw_end']}")

    print(f"\nWill DELETE rosters.id={roster_row['id']} and transactions.id={txn['id']}, "
          f"freeing {manager['name']}'s roster spot, and log an audit_log override entry.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute("DELETE FROM rosters WHERE id=?", (roster_row['id'],))
    conn.execute("DELETE FROM transactions WHERE id=?", (txn['id'],))

    actor_name = 'Admin'
    conn.execute("""
        INSERT INTO audit_log (manager_id, actor_name, entity_type, action, summary, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        manager_id, actor_name, 'roster', 'manual_override_undo',
        f"Manually undid {manager['name']}'s add of {PLAYER_NAME} (rule override, per manager request)",
        json.dumps({"undone_transaction_id": txn['id'], "undone_roster_id": roster_row['id'],
                    "player": PLAYER_NAME, "gw": txn['gw']}),
        datetime.now().isoformat(),
    ))

    conn.commit()
    print("\nApplied.")
    conn.close()


if __name__ == '__main__':
    main()
