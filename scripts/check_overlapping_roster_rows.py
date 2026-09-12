"""
Read-only diagnostic: are there any (manager, player) pairs with two or more
rosters rows whose [gw_start, gw_end] ranges overlap?

Every roster row is supposed to represent one continuous, non-overlapping
stretch of gameweeks a player held a given slot (see apply_slot_change() in
app.py, which always closes the old row at gw_end=gw-1 before opening a new
one). If two rows for the same player ever overlap, then querying "this
player's active row for gw N" (as every roster-view route does) can return
more than one match depending on row ordering -- which would explain a
symptom like a slot dropdown briefly showing a stale value (e.g. "Bench")
for a player whose real, current row is correct, if the wrong one of two
overlapping rows got picked up in some render.

This script only reports -- it makes no changes. Run it to confirm or rule
out a duplicate/overlapping-row bug as the cause of anything resembling
"a player's displayed slot doesn't match their real one."

    python3 check_overlapping_roster_rows.py
"""
import sqlite3

from init_db import DB_PATH


def ranges_overlap(a_start, a_end, b_start, b_end):
    a_end = a_end if a_end is not None else float('inf')
    b_end = b_end if b_end is not None else float('inf')
    return a_start <= b_end and b_start <= a_end


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT r.id, r.manager_id, m.name AS manager_name, r.player_name,
               r.slot_type, r.position_slot, r.gw_start, r.gw_end
        FROM rosters r
        JOIN managers m ON m.id = r.manager_id
        ORDER BY r.manager_id, r.player_name, r.gw_start
    """).fetchall()

    by_key = {}
    for r in rows:
        by_key.setdefault((r['manager_id'], r['player_name']), []).append(r)

    found = 0
    for (manager_id, player_name), group in by_key.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if ranges_overlap(a['gw_start'], a['gw_end'], b['gw_start'], b['gw_end']):
                    found += 1
                    print(f"\nOVERLAP: {player_name} on {a['manager_name']} (manager #{manager_id})")
                    print(f"  row id={a['id']}: {a['slot_type']}/{a['position_slot']} "
                          f"gw_start={a['gw_start']} gw_end={a['gw_end']}")
                    print(f"  row id={b['id']}: {b['slot_type']}/{b['position_slot']} "
                          f"gw_start={b['gw_start']} gw_end={b['gw_end']}")

    print(f"\n{'='*60}")
    if found:
        print(f"{found} overlapping row pair(s) found -- see above.")
    else:
        print("No overlapping roster rows found for any manager/player. "
              "The rosters table's gw ranges are all clean and non-overlapping.")
    conn.close()


if __name__ == '__main__':
    main()
