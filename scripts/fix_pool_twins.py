"""
Follow-up to fix_pool_gaps.py (2026-11-26): the four players it held back
because an existing row looked like the same person under the Fantrax
spelling. Confirmed same club/position in Render Shell, so each existing row
is renamed to the WhoScored spelling (what raw_stats uses -- standing rule:
names match WhoScored, accents included), gets the WhoScored ID read off a
match page he played in, and gains any extra WhoScored-listed positions.

Report only by default; --apply writes in one transaction. Run from scripts/:
    python3 fix_pool_twins.py
    python3 fix_pool_twins.py --apply
"""
import argparse
import sqlite3
import sys

from init_db import DB_PATH
from fix_koumas_duplicate import name_columns, rename_everywhere

# (WhoScored spelling, current players-row spelling, WhoScored ID, WhoScored positions)
TWINS = [
    ("Alysson", "Alysson Edward", 520919, ['MID']),
    ("Allan", "Allan Elias", 521094, ['MID']),
    ("Ferdi Kadıoğlu", "Ferdi Kadioglu", 321958, ['DEF', 'MID']),
    # row had 429565; 429556 is the ID on the match page he played in
    ("Emersonn", "Emersonn Correia da Silva", 429556, ['FW']),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--db', default=DB_PATH, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cols = name_columns(conn)
    players = {r['name']: dict(r) for r in conn.execute("SELECT * FROM players")}
    scraped = {r[0] for r in conn.execute("SELECT DISTINCT player_name FROM raw_stats WHERE external=0")}
    taken_ids = {p['whoscored_id']: n for n, p in players.items() if p.get('whoscored_id')}

    todo, problems = [], []
    for new, old, wsid, positions in TWINS:
        if new in players and old not in players:
            print(f"  {new}: already renamed, skip")
            continue
        if new in players:
            problems.append(f"{new!r} already has a players row -- needs a full merge, skipped")
        elif old not in players:
            problems.append(f"no players row for {old!r}, skipped")
        elif new not in scraped:
            problems.append(f"{new!r} has no raw_stats rows -- check spelling, skipped")
        elif taken_ids.get(wsid) not in (None, old):
            problems.append(f"whoscored_id {wsid} already belongs to {taken_ids[wsid]!r}, skipped")
        else:
            p = players[old]
            todo.append((new, old, wsid, positions, p['id']))
            print(f"  {old!r} -> {new!r} ({p['club']} / {p['position']}), "
                  f"whoscored_id {p['whoscored_id']} -> {wsid}, eligible += {positions}")

    print(f"\nSummary: {len(todo)} to rename.")
    if problems:
        print("Needs a look (skipped):")
        for p in problems:
            print(f"  - {p}")
    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to write.")
        return 0

    try:
        for new, old, wsid, positions, pid in todo:
            rename_everywhere(conn, cols, old, new)  # rosters, projections, etc. (not players.name)
            conn.execute("UPDATE players SET name=?, whoscored_id=?, draftable=1 WHERE id=?", (new, wsid, pid))
            for pos in positions:
                conn.execute("INSERT OR IGNORE INTO player_eligibility (player_id, position, source) "
                             "VALUES (?,?,?)", (pid, pos, 'whoscored_live'))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\nFAILED, rolled back, nothing written: {e}")
        return 1
    print("\nApplied. Re-run report_missing_from_pool.py to confirm what's left.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
