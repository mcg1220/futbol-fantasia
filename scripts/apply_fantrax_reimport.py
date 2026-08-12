"""
Step 2 of the reimport: apply the user's resolutions for the ambiguous +
removed buckets from analyze_fantrax_reimport.py, plus the confirmed
same_club/transfers/new_players buckets automatically.

Inputs (both must already agree with each other -- see the name-set
cross-check this was run with before use):
- /tmp/fantrax_reimport_diff.json   (analyze_fantrax_reimport.py output)
- fantrax_resolutions.txt           (user's disposition, same format the
                                      review Artifact's "Copy" button emits)

- same_club / transfers: upsert player_projections (season 2026-27);
  transfers also update players.club.
- new_players (auto, non-ambiguous): insert as new players.
- ambiguous "MATCH -> X": treat exactly like a transfer/same-club upsert
  against the resolved DB player X.
- ambiguous "NEW PLAYER": insert as new players.
- removed (soft-exclude): players.draftable = 0, drop the now-stale 2026-27
  projection row. Row itself is kept (historical stats/eligibility intact).

Run: python3 apply_fantrax_reimport.py
"""

import json
import re
import sqlite3
from init_db import DB_PATH

SEASON = '2026-27'
DIFF_JSON = '/tmp/fantrax_reimport_diff.json'
import os
RESOLUTIONS_TXT = os.environ.get(
    'FANTRAX_RESOLUTIONS_TXT',
    '/private/tmp/claude-501/-Users-michaelgarcia-futbol-fantasia/'
    '8f9585fa-4f17-4e3c-b9bb-4c2b7103eef2/scratchpad/fantrax_resolutions.txt'
)


def parse_resolutions(path):
    text = open(path).read()
    amb_section, rem_section = text.split('== MARKED FOR SOFT-REMOVAL')

    matches = {}   # fantrax_name -> resolved db_name
    new_player_names = set()
    for line in amb_section.splitlines():
        if not line.startswith('- '):
            continue
        m = re.match(r'- (.+?) \(.+?\): MATCH -> (.+?) \(currently .+?\)', line)
        if m:
            matches[m.group(1)] = m.group(2)
            continue
        m = re.match(r'- (.+?) \(.+?\): NEW PLAYER', line)
        if m:
            new_player_names.add(m.group(1))

    removed_names = set()
    for line in rem_section.splitlines():
        m = re.match(r'- (.+?) \(last: .+?\)', line)
        if m:
            removed_names.add(m.group(1))

    return matches, new_player_names, removed_names


def upsert_projection(conn, name, fpts, fpg):
    conn.execute("""
        INSERT INTO player_projections (season, player_name, proj_total, proj_avg)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(season, player_name) DO UPDATE SET
            proj_total = excluded.proj_total, proj_avg = excluded.proj_avg
    """, (SEASON, name, float(fpts or 0), float(fpg or 0)))


def insert_new_player(conn, name, club, position, fpts, fpg):
    cur = conn.execute(
        "INSERT INTO players (name, club, position) VALUES (?, ?, ?)",
        (name, club, position)
    )
    conn.execute(
        "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
        (cur.lastrowid, position, 'fantrax_reimport_2026')
    )
    upsert_projection(conn, name, fpts, fpg)


def main():
    diff = json.load(open(DIFF_JSON))
    matches, new_player_names, removed_names = parse_resolutions(RESOLUTIONS_TXT)

    conn = sqlite3.connect(DB_PATH)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
    if 'draftable' not in cols:
        conn.execute("ALTER TABLE players ADD COLUMN draftable INTEGER NOT NULL DEFAULT 1")
        print("Added players.draftable column")

    print(f"1. Same-club upserts ({len(diff['same_club'])})...")
    for r in diff['same_club']:
        upsert_projection(conn, r['db_name'], r['fpts'], r['fpg'])

    print(f"2. Transfers -- club update + upsert ({len(diff['transfers'])})...")
    club_updates = 0
    for r in diff['transfers']:
        conn.execute("UPDATE players SET club=? WHERE name=?", (r['fantrax_club'], r['db_name']))
        club_updates += 1
        upsert_projection(conn, r['db_name'], r['fpts'], r['fpg'])
    print(f"   {club_updates} club rows updated")

    print(f"3. Auto-confirmed new players ({len(diff['new_players'])})...")
    for r in diff['new_players']:
        insert_new_player(conn, r['fantrax_name'], r['fantrax_club'],
                           r['fantrax_positions'][0], r['fpts'], r['fpg'])

    amb_by_name = {r['fantrax_name']: r for r in diff['ambiguous']}
    missing_matches = [f for f in matches if f not in amb_by_name]
    missing_new = [f for f in new_player_names if f not in amb_by_name]
    if missing_matches or missing_new:
        raise SystemExit(f"Resolution/diff mismatch -- missing_matches={missing_matches} missing_new={missing_new}")

    print(f"4. Ambiguous MATCH resolutions ({len(matches)})...")
    for fantrax_name, db_name in matches.items():
        r = amb_by_name[fantrax_name]
        row = conn.execute("SELECT club FROM players WHERE name=?", (db_name,)).fetchone()
        if not row:
            raise SystemExit(f"MATCH target '{db_name}' (for '{fantrax_name}') not found in players table")
        if row[0] != r['fantrax_club']:
            conn.execute("UPDATE players SET club=? WHERE name=?", (r['fantrax_club'], db_name))
        upsert_projection(conn, db_name, r['fpts'], r['fpg'])

    print(f"5. Ambiguous NEW PLAYER resolutions ({len(new_player_names)})...")
    for fantrax_name in new_player_names:
        r = amb_by_name[fantrax_name]
        insert_new_player(conn, r['fantrax_name'], r['fantrax_club'],
                           r['fantrax_positions'][0], r['fpts'], r['fpg'])

    removed_by_name = {r['db_name']: r for r in diff['removed']}
    missing_removed = [n for n in removed_names if n not in removed_by_name]
    if missing_removed:
        raise SystemExit(f"Resolution/diff mismatch on removed -- missing={missing_removed}")

    print(f"6. Soft-excluding removed players ({len(removed_names)})...")
    for db_name in removed_names:
        conn.execute("UPDATE players SET draftable=0 WHERE name=?", (db_name,))
        conn.execute("DELETE FROM player_projections WHERE season=? AND player_name=?", (SEASON, db_name))

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == '__main__':
    main()
