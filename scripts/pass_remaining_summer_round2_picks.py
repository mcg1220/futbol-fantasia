"""
One-off admin override: pass on every remaining Round 2 pick of the Summer
Transfer Draft, so the draft can be closed out while the managers holding
those picks are away from their computers. Per the commissioner: everyone
still on the clock has confirmed (outside the app) they won't use their
Round 2 pick.

Mirrors exactly what /api/transfer/<type>/pass does server-side (same
overall_pick formula, same advance-the-clock logic) but skips the
manager-identity check that route enforces, since no one is actually
clicking "Pass" themselves here -- this is a commissioner override, same
spirit as scripts/undo_schade_transaction.py's rule override.

Dry run by default -- prints exactly who it would pass for and in what
order, does not write anything until re-run with --apply. Safe to re-run:
if the draft has already completed (or someone actually made a real pick
in the meantime), it stops and reports the current state instead of
guessing.

Run:
    python3 pass_remaining_summer_round2_picks.py            # dry run
    python3 pass_remaining_summer_round2_picks.py --apply    # writes
"""
import argparse
import sqlite3
from datetime import datetime

from init_db import DB_PATH

SEASON = '2026-27'
DRAFT_TYPE = 'summer'
DRAFT_TEAMS = 8


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually write. Without this, dry-run only.')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    draft = c.execute(
        "SELECT * FROM transfer_drafts WHERE season=? AND draft_type=?", (SEASON, DRAFT_TYPE)
    ).fetchone()
    if not draft:
        print("No summer transfer draft found for this season. Aborting.")
        return
    if draft['status'] != 'in_progress' or draft['round'] != 2:
        print(f"Draft is status={draft['status']!r} round={draft['round']} -- expected in_progress/round 2. "
              f"State may have changed since this was written; re-check before running. Aborting.")
        return

    order = c.execute("""
        SELECT tdo.position, tdo.manager_id, m.name, m.team_name
        FROM transfer_draft_order tdo JOIN managers m ON m.id = tdo.manager_id
        WHERE tdo.transfer_draft_id=? AND tdo.round=2
        ORDER BY tdo.position
    """, (draft['id'],)).fetchall()

    remaining = [o for o in order if o['position'] >= draft['current_pick_number']]
    if not remaining:
        print("No remaining Round 2 picks -- draft should already be complete. Aborting.")
        return

    print(f"Will pass on Round 2, positions {remaining[0]['position']}-{remaining[-1]['position']}, in order:")
    for o in remaining:
        print(f"  #{o['position']}: {o['name']} — {o['team_name']}")
    print(f"\nAfter the last pass, the draft will be marked complete.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    now = datetime.now().isoformat()
    pick_number = draft['current_pick_number']
    for o in remaining:
        overall_pick = (2 - 1) * DRAFT_TEAMS + pick_number  # same formula as transfer_draft_pass()
        conn.execute("""
            INSERT INTO transfer_draft_picks (transfer_draft_id, round, overall_pick, manager_id, player_name, dropped_player, is_pass, picked_at)
            VALUES (?,2,?,?,NULL,NULL,1,?)
        """, (draft['id'], overall_pick, o['manager_id'], now))
        conn.execute("""
            INSERT INTO audit_log (manager_id, actor_name, entity_type, action, summary, detail_json, created_at)
            VALUES (?, 'Admin', 'transfer_draft', 'pass', ?, NULL, ?)
        """, (o['manager_id'], f"Summer transfer draft: passed (round 2) — commissioner override, "
                                f"manager confirmed outside the app they won't use this pick", now))
        print(f"Passed for {o['name']} (#{o['position']})")
        pick_number += 1

    conn.execute(
        "UPDATE transfer_drafts SET round=2, current_pick_number=?, status='complete', completed_at=? WHERE id=?",
        (remaining[-1]['position'], now, draft['id'])
    )
    conn.commit()
    print("\nApplied. Summer Transfer Draft is now complete.")
    conn.close()


if __name__ == '__main__':
    main()
