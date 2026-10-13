"""
One-off: Lewis Koumas (Liverpool) exists twice -- "Lewis Koumas" (WhoScored's
spelling, which every scraped raw_stats row is stored under) and "Louie
Koumas" (Fantrax's spelling, which is what got drafted/rostered). Nothing
joins the two, so the rostered "Louie" shows 0.00 every week while "Lewis"
quietly accumulates the real points -- the same duplicate-player bug as
Andy/Andrew Robertson and Sávio.

The surviving spelling is whichever one raw_stats actually uses (scraper
ground truth, which is also WhoScored's spelling -- the standing rule is to
conform to WhoScored, never the other way round). raw_stats itself is never
modified.

Differences from fix_savio.py, which this follows:
  * the two spellings are an explicit alias list -- "Louie"/"Lewis" is not
    an accent difference, so accent-normalising wouldn't link them;
  * every table/column that stores a player name is discovered from the live
    schema rather than a hard-coded list, so newer tables aren't missed;
  * where a rename collides with a UNIQUE constraint (e.g. both spellings
    have a shortlist entry / projection / start-status row) the keeper's row
    wins, but only in tables where that's clearly the right call -- anywhere
    else it aborts and writes nothing rather than guessing;
  * refuses to merge if the two spellings are actively rostered by
    different managers.

The last section is a read-only sweep for OTHER currently-rostered players
with no raw_stats rows under their exact name, with likely spelling matches,
so any further mismatches of this kind are visible in the same run.

Run: python3 fix_koumas_duplicate.py            # report only, writes nothing
     python3 fix_koumas_duplicate.py --apply    # apply the merge
"""
import argparse
import difflib
import sqlite3
import sys
import unicodedata

from init_db import DB_PATH

SEASON = '2026-27'
# Same person. First entry is the fallback survivor if raw_stats has neither.
ALIASES = ['Lewis Koumas', 'Louie Koumas']
CORRECT_CLUB = 'Liverpool'
FALLBACK_POSITION = 'FW'

NAME_COLUMNS = {'player_name', 'add_player', 'drop_player', 'added_player', 'dropped_player'}
NEVER_TOUCH_TABLES = {'raw_stats'}  # scraper ground truth
# Tables where, if the rename collides with a UNIQUE row for the survivor,
# dropping the old-spelling row (survivor's wins) is the correct resolution.
DEDUPE_TABLES = {'player_projections', 'player_start_status', 'shortlists',
                 'transfer_pool', 'ir_eligibility_overrides'}


def normalize(name):
    d = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().strip()


def name_columns(conn):
    out = []
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table in NEVER_TOUCH_TABLES:
            continue
        for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
            if col[1] in NAME_COLUMNS:
                out.append((table, col[1]))
    return out


def count_refs(conn, cols, name):
    refs = {}
    for table, col in cols:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (name,)).fetchone()[0]
        if n:
            refs[f"{table}.{col}"] = n
    return refs


def rename_everywhere(conn, cols, old, new):
    for table, col in cols:
        cur = conn.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old))
        moved = cur.rowcount
        left = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (old,)).fetchone()[0]
        if left:
            if table in DEDUPE_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE {col}=?", (old,))
                print(f"  {table}.{col}: {moved} renamed, {left} duplicate row(s) dropped (survivor's row kept)")
                continue
            raise RuntimeError(
                f"{table}.{col}: {left} row(s) for {old!r} could not be renamed (unique conflict) -- "
                f"aborting, nothing written")
        if moved:
            print(f"  {table}.{col}: renamed {moved} row(s)")


def report_other_mismatches(conn):
    """Read-only: currently-rostered names with no raw_stats rows under that
    exact name, plus the closest spellings that DO have rows."""
    rostered = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT player_name FROM rosters WHERE gw_end IS NULL")})
    raw_names = {r[0] for r in conn.execute(
        "SELECT DISTINCT player_name FROM raw_stats WHERE external=0")}
    by_surname = {}
    for n in raw_names:
        by_surname.setdefault(normalize(n).split()[-1] if n.split() else n, set()).add(n)

    print("\n" + "=" * 70)
    print("Sweep: currently-rostered players with NO raw_stats rows under their exact name")
    print("(either a spelling mismatch like this one, or just not in a matchday squad yet)")
    print("=" * 70)
    found = 0
    for name in rostered:
        if name in raw_names:
            continue
        cands = set(difflib.get_close_matches(name, raw_names, n=3, cutoff=0.72))
        cands |= by_surname.get(normalize(name).split()[-1], set())
        found += 1
        print(f"  {name!r:32} -> likely: {sorted(cands) if cands else 'no similar name in raw_stats'}")
    if not found:
        print("  none -- every rostered player has raw_stats rows under their exact name")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='apply the merge (default: report only)')
    ap.add_argument('--db', default=DB_PATH, help=argparse.SUPPRESS)  # for testing against a scratch copy
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cols = name_columns(conn)

    players = conn.execute(
        f"SELECT * FROM players WHERE name IN ({','.join('?' * len(ALIASES))})", ALIASES).fetchall()
    raw = {r['player_name']: r for r in conn.execute(f"""
        SELECT rs.player_name, COUNT(DISTINCT rs.match_id) AS matches, MIN(g.gw_number) AS first_gw,
               MAX(g.gw_number) AS last_gw
        FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season=? AND rs.external=0 AND rs.player_name IN ({','.join('?' * len(ALIASES))})
        GROUP BY rs.player_name""", [SEASON] + ALIASES).fetchall()}

    print(f"players rows for {ALIASES}: {len(players)}")
    for p in players:
        elig = [r[0] for r in conn.execute(
            "SELECT position FROM player_eligibility WHERE player_id=?", (p['id'],))]
        print(f"  #{p['id']} {p['name']!r}: club={p['club']!r} position={p['position']!r} "
              f"whoscored_id={p['whoscored_id']} draftable={p['draftable']} eligible={elig}")
    print(f"\nraw_stats appearances this season ({SEASON}):")
    for a in ALIASES:
        r = raw.get(a)
        print(f"  {a!r}: " + (f"{r['matches']} match(es), GW{r['first_gw']}-GW{r['last_gw']}" if r else "none"))
    print("\nroster history (manager, slot, position_slot, gw_start-gw_end; NULL end = still active):")
    for r in conn.execute(f"""SELECT manager_id, player_name, slot_type, position_slot, gw_start, gw_end FROM rosters
                              WHERE player_name IN ({','.join('?' * len(ALIASES))}) ORDER BY gw_start""", ALIASES):
        print(f"  manager {r['manager_id']}: {r['player_name']!r} {r['slot_type']}/{r['position_slot']} "
              f"GW{r['gw_start']}-{r['gw_end'] if r['gw_end'] is not None else 'now'}")
    print("\nreferences by spelling (table.column: rows):")
    for a in ALIASES:
        print(f"  {a!r}: {count_refs(conn, cols, a) or 'none'}")

    if not players:
        print("\nNeither spelling has a players row -- not something this script handles. Aborting.")
        return 1

    if len(raw) > 1:
        print("\nBoth spellings have scraped appearances this season -- ambiguous, needs a human. Aborting.")
        return 1
    final_name = next(iter(raw)) if raw else ALIASES[0]

    active = conn.execute(f"""
        SELECT DISTINCT manager_id FROM rosters
        WHERE gw_end IS NULL AND player_name IN ({','.join('?' * len(ALIASES))})""", ALIASES).fetchall()
    if len(active) > 1:
        print(f"\nBoth spellings are actively rostered by different managers "
              f"({[a['manager_id'] for a in active]}) -- refusing to merge. Aborting.")
        return 1

    keeper = next((p for p in players if p['name'] == final_name), None) or players[0]
    others = [p for p in players if p['id'] != keeper['id']]
    renames = sorted({p['name'] for p in players if p['name'] != final_name})

    print(f"\nPlan: keep spelling {final_name!r} (players row #{keeper['id']}), "
          f"club={CORRECT_CLUB!r}, draftable=1")
    for r in renames:
        print(f"  repoint every {r!r} reference -> {final_name!r}")
    for o in others:
        print(f"  merge players row #{o['id']} {o['name']!r} into #{keeper['id']} (eligibility unioned) "
              f"then delete it")
    if not renames and not others and keeper['club'] == CORRECT_CLUB and keeper['draftable'] == 1:
        print("  (nothing to do -- already merged)")

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to write.")
        report_other_mismatches(conn)
        return 0

    try:
        for r in renames:
            rename_everywhere(conn, cols, r, final_name)
        for o in others:
            for pos, source in conn.execute(
                    "SELECT position, source FROM player_eligibility WHERE player_id=?", (o['id'],)).fetchall():
                conn.execute("INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                             (keeper['id'], pos, source))
            conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (o['id'],))
            conn.execute("DELETE FROM players WHERE id=?", (o['id'],))
        if keeper['name'] != final_name:
            conn.execute("UPDATE players SET name=? WHERE id=?", (final_name, keeper['id']))
        other_ws = next((o['whoscored_id'] for o in others if o['whoscored_id']), None)
        other_pos = next((o['position'] for o in others if o['position']), None)
        conn.execute(
            "UPDATE players SET club=?, position=COALESCE(NULLIF(position,''), ?, ?), "
            "whoscored_id=COALESCE(whoscored_id, ?), draftable=1 WHERE id=?",
            (CORRECT_CLUB, other_pos, FALLBACK_POSITION, other_ws, keeper['id']))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\nFAILED, rolled back, nothing written: {e}")
        return 1

    print("\nApplied.")
    leftovers = {a: count_refs(conn, cols, a) for a in renames}
    print(f"leftover references to old spelling(s): {leftovers}")
    report_other_mismatches(conn)
    return 0


if __name__ == '__main__':
    sys.exit(main())
