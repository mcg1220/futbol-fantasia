"""
One-off: restore Mohamed Belloumi (Nick's team) to his Midfield starter slot
for GW4. Nick reported that after moving other, unlocked players around on
his Team page, the slot dropdown for already-locked players (Belloumi
included) visually flipped to "Bench" even though their position in the
lineup table didn't move -- possibly just a frontend display glitch, but
this script checks (and if needed, corrects) the actual rosters row so we
know for certain the real, persisted data is right regardless of what the
UI showed.

Bypasses the normal lock check on purpose (the whole reason this needs a
script instead of the Team page UI is that Belloumi's match has already
kicked off, so the app correctly refuses to let anyone touch his slot
through the normal endpoint anymore) -- this is a deliberate admin
correction of what should have stayed true the whole time, not a new
lineup change.

    python3 fix_belloumi_position.py            # dry run (reports only)
    python3 fix_belloumi_position.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

MANAGER_NAME = 'Nick'
PLAYER_NAME = 'Mohamed Belloumi'
SEASON = '2026-27'
GW = 4
TARGET_SLOT_TYPE = 'starter'
TARGET_POSITION_SLOT = 'MID'


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
    print(f"Manager: #{manager['id']} {manager['name']} ({manager['team_name']})")

    row = conn.execute("""
        SELECT id, slot_type, position_slot, gw_start, gw_end FROM rosters
        WHERE manager_id=? AND player_name=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
    """, (manager['id'], PLAYER_NAME, GW, GW)).fetchone()
    if not row:
        print(f"{PLAYER_NAME} has no active roster row for GW{GW} on this team -- nothing to do.")
        return

    print(f"\nCurrent GW{GW} row: id={row['id']} slot_type={row['slot_type']!r} "
          f"position_slot={row['position_slot']!r} (covers gw_start={row['gw_start']}, gw_end={row['gw_end']})")

    starter_counts = conn.execute("""
        SELECT position_slot, COUNT(*) as cnt FROM rosters
        WHERE manager_id=? AND slot_type='starter' AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        GROUP BY position_slot
    """, (manager['id'], GW, GW)).fetchall()
    print(f"Current GW{GW} starter counts: {dict((r['position_slot'], r['cnt']) for r in starter_counts)}")

    if row['slot_type'] == TARGET_SLOT_TYPE and row['position_slot'] == TARGET_POSITION_SLOT:
        print(f"\n{PLAYER_NAME} is already {TARGET_SLOT_TYPE}/{TARGET_POSITION_SLOT} for GW{GW} -- no fix needed.")
        return

    print(f"\nWould change to: slot_type={TARGET_SLOT_TYPE!r} position_slot={TARGET_POSITION_SLOT!r}")

    mid_count = next((r['cnt'] for r in starter_counts if r['position_slot'] == TARGET_POSITION_SLOT), 0)
    if TARGET_SLOT_TYPE == 'starter' and mid_count >= 4:
        print(f"\nWARNING: this manager already has {mid_count} MID starters for GW{GW} -- "
              f"restoring Belloumi would make {mid_count + 1}. Double-check the lineup before applying.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    if row['gw_start'] == GW:
        conn.execute("UPDATE rosters SET slot_type=?, position_slot=? WHERE id=?",
                     (TARGET_SLOT_TYPE, TARGET_POSITION_SLOT, row['id']))
    else:
        # Same split used by apply_slot_change() in app.py: close the
        # existing row the gw before this fix, open a fresh one starting at
        # GW carrying the corrected slot forward, inheriting whatever
        # gw_end the original row already had.
        conn.execute("UPDATE rosters SET gw_end=? WHERE id=?", (GW - 1, row['id']))
        conn.execute("""
            INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (manager['id'], PLAYER_NAME, TARGET_SLOT_TYPE, TARGET_POSITION_SLOT, GW, row['gw_end']))
    conn.commit()
    print("\nApplied.")
    conn.close()


if __name__ == '__main__':
    main()
