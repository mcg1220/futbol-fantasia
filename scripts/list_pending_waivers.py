"""
Read-only report: what's sitting on the waiver wire right now, so the
commissioner can see what will actually process before closing a window.

Two separate things can be "pending":
  - waiver_claims (status='pending') under the current OPEN window -- these
    are what /api/waiver/process actually resolves when you close the
    window.
  - pending_waiver_claims -- claims a manager queued while NO window was
    open (e.g. a locked free-agent pickup that got auto-converted to a
    waiver). These only get promoted into a real window's waiver_claims
    when the NEXT window OPENS, not when one closes -- so if a window is
    already open, this list should normally be empty (already promoted).

Nothing is written -- this is purely a report.

Run: python3 list_pending_waivers.py [--season 2026-27]
"""
import argparse
import sqlite3

from init_db import DB_PATH

DEFAULT_SEASON = '2026-27'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--season', default=DEFAULT_SEASON)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    window = c.execute("""
        SELECT * FROM waiver_windows WHERE season=? ORDER BY window_number DESC LIMIT 1
    """, (args.season,)).fetchone()

    if not window:
        print(f"No waiver window has ever been opened for {args.season}.")
    elif window['status'] != 'open':
        print(f"Latest window (#{window['window_number']}) is status={window['status']!r} -- no window currently open.")
    else:
        claims = c.execute("""
            SELECT wc.*, m.name AS manager_name, m.team_name
            FROM waiver_claims wc JOIN managers m ON m.id = wc.manager_id
            WHERE wc.window_id=? AND wc.status='pending'
            ORDER BY wc.sequence_number, wc.priority
        """, (window['id'],)).fetchall()

        print(f"Window #{window['window_number']} is OPEN (opened {window['opened_at']}). "
              f"{len(claims)} pending claim(s) will be attempted when it's closed:\n")
        if not claims:
            print("  (none)")
        else:
            by_manager = {}
            for cl in claims:
                by_manager.setdefault((cl['manager_name'], cl['team_name']), []).append(cl)
            for (name, team), rows in by_manager.items():
                print(f"  {name} — {team} (waiver priority order: {', '.join(str(r['priority']) for r in rows)})")
                for r in rows:
                    ir_note = " → IR" if r['to_ir'] else ""
                    drop = f", dropping {r['drop_player']}" if r['drop_player'] else " (pure add)"
                    print(f"      priority {r['priority']}: claim {r['add_player']}{ir_note}{drop}")

    pending = c.execute("""
        SELECT pwc.*, m.name AS manager_name, m.team_name
        FROM pending_waiver_claims pwc JOIN managers m ON m.id = pwc.manager_id
        WHERE pwc.season=?
        ORDER BY pwc.created_at
    """, (args.season,)).fetchall()

    print(f"\n{len(pending)} claim(s) queued with no window open (these promote into the *next* window "
          f"when it OPENS, not when one closes):")
    if not pending:
        print("  (none)")
    else:
        for p in pending:
            ir_note = " → IR" if p['to_ir'] else ""
            drop = f", dropping {p['drop_player']}" if p['drop_player'] else " (pure add)"
            print(f"  {p['manager_name']} — {p['team_name']}: claim {p['add_player']}{ir_note}{drop} (queued {p['created_at']})")

    conn.close()


if __name__ == '__main__':
    main()
