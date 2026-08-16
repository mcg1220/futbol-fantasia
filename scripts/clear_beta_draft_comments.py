"""
One-time cleanup: clear pre-prod beta-test comments off the Draft Recap page.

The main draft is now locked, but every comment currently in draft_comments
was left during beta testing before the real draft happened -- confirmed by
reading the actual rows (one literally says "this was a test comment in
beta"). No real post-draft commentary exists yet, so this is a full wipe for
the current season rather than a filtered cleanup.

    python3 clear_beta_draft_comments.py            # dry run
    python3 clear_beta_draft_comments.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

SEASON = '2026-27'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, target_type, target_id, author_name, comment, created_at "
        "FROM draft_comments WHERE season=? ORDER BY created_at", (SEASON,)
    ).fetchall()

    if not rows:
        print(f"No draft_comments rows for season {SEASON} — nothing to do.")
        return

    print(f"draft_comments rows for season {SEASON} ({len(rows)}):")
    for r in rows:
        print(f"  #{r['id']} [{r['target_type']}:{r['target_id']}] {r['author_name']}: "
              f"{r['comment']!r}  ({r['created_at']})")

    if not args.apply:
        print("\nDry run. Re-run with --apply to delete these rows.")
        return

    conn.execute("DELETE FROM draft_comments WHERE season=?", (SEASON,))
    conn.commit()
    print(f"\nDeleted {len(rows)} row(s) from draft_comments for season {SEASON}.")
    conn.close()


if __name__ == '__main__':
    main()
