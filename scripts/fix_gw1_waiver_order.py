"""
One-time fix: correct the pre-GW1 waiver order to true reverse Round-1 draft
order.

seed_waiver_order_if_needed() (app.py) is supposed to do exactly this, but
it derives the order from draft_order.pick_slot -- the ORIGINAL randomizer
slot assigned before the draft, reversed. That's wrong whenever any Round 1
picks were traded before the draft started (this league has a Trade Picks
feature), since pick_slot doesn't move with a traded pick -- only
draft_picks.manager_id (the actual final owner) does. Symptom: the seeded
waiver order didn't match reverse Round 1 results at all.

This script derives the order from draft_picks itself -- the actual,
already-final record of who picked what -- which is authoritative
regardless of any pre-draft trading. Round 1, ordered by overall_pick
descending, gives last-pick-first (true reverse draft order): the manager
who picked last in Round 1 gets waiver priority #1.

Only touches the pre-GW1 window: refuses to run if a waiver window is
already open (once real waiver activity starts, order changes via normal
claim processing -- successful claim drops you to the bottom -- and this
script has no business overwriting that).

    python3 fix_gw1_waiver_order.py            # dry run
    python3 fix_gw1_waiver_order.py --apply
"""
import argparse
import sqlite3
import sys

from init_db import DB_PATH

SEASON = '2026-27'
DRAFT_TEAMS = 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    open_window = conn.execute(
        "SELECT 1 FROM waiver_windows WHERE season=? AND status='open'", (SEASON,)
    ).fetchone()
    if open_window:
        sys.exit("A waiver window is currently open — refusing to overwrite the order. "
                 "This script is only for the pre-GW1 seed.")

    round1 = conn.execute("""
        SELECT dp.overall_pick, dp.manager_id, m.name, m.team_name
        FROM draft_picks dp JOIN managers m ON m.id = dp.manager_id
        WHERE dp.season=? AND dp.round=1
        ORDER BY dp.overall_pick
    """, (SEASON,)).fetchall()

    if len(round1) != DRAFT_TEAMS:
        sys.exit(f"Expected {DRAFT_TEAMS} Round 1 picks, found {len(round1)} — "
                 "draft may not be far enough along. Aborting.")

    reverse_order = list(reversed(round1))

    print("Round 1 draft order (actual, post-trade):")
    for r in round1:
        print(f"  pick {r['overall_pick']}: {r['name']} ({r['team_name']})")

    print("\nCorrect waiver order (reverse Round 1):")
    for i, r in enumerate(reverse_order, start=1):
        print(f"  #{i}: {r['name']} ({r['team_name']})  [was pick {r['overall_pick']}]")

    current = conn.execute("""
        SELECT wo.position, m.name FROM waiver_order wo
        JOIN managers m ON m.id = wo.manager_id
        WHERE wo.season=? ORDER BY wo.position
    """, (SEASON,)).fetchall()
    if current:
        print("\nCurrently stored waiver order (about to be replaced):")
        for r in current:
            print(f"  #{r['position']}: {r['name']}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute("DELETE FROM waiver_order WHERE season=?", (SEASON,))
    for i, r in enumerate(reverse_order, start=1):
        conn.execute(
            "INSERT INTO waiver_order (season, manager_id, position) VALUES (?, ?, ?)",
            (SEASON, r['manager_id'], i)
        )
    conn.commit()
    print(f"\nApplied — waiver_order for {SEASON} now set to true reverse Round 1 order.")
    conn.close()


if __name__ == '__main__':
    main()
