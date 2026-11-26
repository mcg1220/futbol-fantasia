"""
One-off (2026-11-26): WhoScored changed Ferdi Kadıoğlu's spelling mid-season.
Every raw_stats row through GW10 (and all of 2025-26) is stored as
"Ferdi Kadioglu"; from GW11 the scraper gets "Ferdi Kadıoğlu", which is what
fix_pool_twins.py renamed his players row, roster etc. to. That left the
older rows orphaned: his Add/Drop totals and history miss GW1-10. (Closed-GW
matchup scores are stored in `results`, so no one's past points changed.)

This brings the older raw_stats rows onto WhoScored's current spelling so
past and future scrapes all join to the one player. Stat values are not
touched -- only the name. Normally raw_stats is never edited, but both
spellings came from WhoScored; this conforms to the one it uses now.

Report only by default; --apply writes in one transaction. Run from scripts/:
    python3 fix_kadioglu_stats.py
    python3 fix_kadioglu_stats.py --apply
"""
import argparse
import sqlite3
import sys

from init_db import DB_PATH
from fix_koumas_duplicate import name_columns, count_refs

OLD, NEW = "Ferdi Kadioglu", "Ferdi Kadıoğlu"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--db', default=DB_PATH, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)

    n_old = conn.execute("SELECT COUNT(*) FROM raw_stats WHERE player_name=?", (OLD,)).fetchone()[0]
    n_new = conn.execute("SELECT COUNT(*) FROM raw_stats WHERE player_name=?", (NEW,)).fetchone()[0]
    clash = conn.execute("""SELECT COUNT(*) FROM raw_stats a JOIN raw_stats b
                            ON a.match_id=b.match_id AND a.player_name=? AND b.player_name=?""",
                         (OLD, NEW)).fetchone()[0]
    has_new_row = conn.execute("SELECT 1 FROM players WHERE name=?", (NEW,)).fetchone()
    has_old_row = conn.execute("SELECT 1 FROM players WHERE name=?", (OLD,)).fetchone()
    other_refs = count_refs(conn, name_columns(conn), OLD)  # raw_stats excluded

    print(f"raw_stats rows: {n_old} under {OLD!r}, {n_new} under {NEW!r}")
    print(f"players row under {NEW!r}: {'yes' if has_new_row else 'NO'}; "
          f"under {OLD!r}: {'YES' if has_old_row else 'no'}")
    if other_refs:
        print(f"other tables still using {OLD!r}: {other_refs}")

    problems = []
    if not has_new_row:
        problems.append(f"no players row for {NEW!r}")
    if has_old_row:
        problems.append(f"a players row for {OLD!r} still exists")
    if clash:
        problems.append(f"{clash} match(es) have rows under both spellings")
    if not n_old:
        print("Nothing to do.")
        return 0
    if problems:
        print("Not safe to run, nothing written:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"\nWill rename {n_old} raw_stats row(s) {OLD!r} -> {NEW!r}.")
    if not args.apply:
        print("Dry run -- nothing written. Re-run with --apply to write.")
        return 0
    try:
        cur = conn.execute("UPDATE raw_stats SET player_name=? WHERE player_name=?", (NEW, OLD))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"FAILED, rolled back, nothing written: {e}")
        return 1
    print(f"Applied: {cur.rowcount} row(s) renamed. Re-run report_missing_from_pool.py.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
