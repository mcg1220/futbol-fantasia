"""
One-off cleanup of players who have stats this season but aren't pickable in
Add/Drop (found by report_missing_from_pool.py, 2026-11-25). Four parts:

  1. MAKE PICKABLE  -- players soft-excluded pre-season (draftable=0) who
                       have since appeared in matchday squads.
  2. SPELLING TWINS -- players row under the Fantrax spelling while every
                       scraped stat is under WhoScored's. Rename to the
                       WhoScored spelling everywhere (standing rule: names
                       conform to WhoScored, accents included).
  3. NEW PLAYERS    -- no players row at all. Added with the club they play
                       for and WhoScored positions, scraped locally into
                       new_player_positions.json by scrape_new_player_positions.py.
                       Skipped (and reported) if an existing row looks like
                       the same person, so nothing gets double-added.
  4. JUNK ROWS      -- Arabic-script raw_stats rows from match 1983599
                       (GW6 Everton v Hull): a broken duplicate scrape with
                       most stats missing. The English rows for the same match
                       are complete and are what rosters were scored from.

Report only by default; --apply writes, all in one transaction (any error
rolls everything back). Run from scripts/ in Render Shell:
    python3 fix_pool_gaps.py
    python3 fix_pool_gaps.py --apply
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata

from init_db import DB_PATH
from fix_koumas_duplicate import name_columns, rename_everywhere

MAKE_PICKABLE = [
    "Yehor Yarmoliuk", "Trey Nyoni", "George Hemmings", "Kepa Arrizabalaga",
    "Ben Gannon-Doak", "Bradley Burrowes", "Triston Rowe", "Tymur Tutierov",
    "Junior Kroupi", "Brian Madjo", "Charly Alcaraz", "Kaine Kesler-Hayden",
    "Braiden Graham", "Callum Olusesi", "Mason Miley", "Miodrag Pivas",
    "Nehemiah Oriola", "Vakhtang Salia",
]

# (WhoScored spelling -- what raw_stats uses, the survivor) <- (current players-row spelling)
SPELLING_TWINS = [
    ("Sidiki Cherif", "Sidiki Chérif"),
    ("Luca Williams-Barnett", "Lucá Williams-Barnett"),
    ("Jens Hjertø-Dahl", "Jens Hjerto-Dahl"),
    ("Jocelin Ta Bi", "Djiamgone Jocelin Ta Bi"),
    ("Modou Kéba Cissé", "Modou Cisse"),
]

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'new_player_positions.json')

JUNK_MATCH_ID = 1983599
ARABIC = re.compile(r'[؀-ۿ]')


def norm(s):
    d = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().replace('ı', 'i').strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--db', default=DB_PATH, help=argparse.SUPPRESS)
    ap.add_argument('--positions', default=POSITIONS_FILE, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cols = name_columns(conn)
    players = {r['name']: dict(r) for r in conn.execute("SELECT * FROM players")}
    by_wsid = {p['whoscored_id']: n for n, p in players.items() if p.get('whoscored_id')}
    by_norm = {}
    for n in players:
        by_norm.setdefault(norm(n), []).append(n)
    scraped = {r[0] for r in conn.execute("SELECT DISTINCT player_name FROM raw_stats WHERE external=0")}
    problems = []

    # 1. make pickable
    print("=== 1. MAKE PICKABLE (draftable 0 -> 1) ===")
    pickable = []
    for n in MAKE_PICKABLE:
        p = players.get(n)
        if not p:
            problems.append(f"make-pickable: no players row for {n!r}")
        elif p['draftable'] == 1:
            print(f"  {n}: already pickable, skip")
        else:
            pickable.append(n)
            print(f"  {n} ({p['club']} / {p['position']})")

    # 2. spelling twins
    print("\n=== 2. SPELLING TWINS (rename to WhoScored spelling) ===")
    twins = []
    for new, old in SPELLING_TWINS:
        if new in players:
            problems.append(f"twin: {new!r} already has a players row -- needs a full merge, skipped")
        elif old not in players:
            problems.append(f"twin: no players row for {old!r}, skipped")
        elif new not in scraped:
            problems.append(f"twin: {new!r} has no raw_stats rows -- check spelling, skipped")
        else:
            twins.append((new, old))
            p = players[old]
            print(f"  {old!r} -> {new!r} ({p['club']} / {p['position']}, draftable={p['draftable']})")

    # 3. new players
    print("\n=== 3. NEW PLAYERS ===")
    with open(args.positions, encoding='utf-8') as f:
        scraped_pos = json.load(f)
    to_add = []
    for n, info in scraped_pos.items():
        if n in players:
            print(f"  {n}: players row already exists, skip")
            continue
        twin = by_wsid.get(info.get('whoscored_id')) or next(iter(by_norm.get(norm(n), [])), None)
        first = norm(n).split()[0] if n.split() else ''
        prefix = [x for x in players if len(n.split()) == 1 and norm(x).split()[0] == first]
        if twin or prefix:
            problems.append(f"new: {n!r} may already exist as {twin or prefix} -- skipped, needs a look")
            continue
        if not info.get('primary') or not info.get('club'):
            problems.append(f"new: {n!r} missing position/club from scrape ({info.get('error')}) -- skipped")
            continue
        to_add.append((n, info))
        print(f"  {n:26} {info['club']:16} {info['primary']:4} eligible={info['positions']} "
              f"whoscored_id={info['whoscored_id']}")

    # 4. junk rows
    print(f"\n=== 4. JUNK ROWS (Arabic-script names, match {JUNK_MATCH_ID}) ===")
    junk = [r for r in conn.execute(
        "SELECT id, player_name, club, minutes_played FROM raw_stats WHERE match_id=?", (JUNK_MATCH_ID,))
        if ARABIC.search(r['player_name'] or '')]
    for r in junk:
        print(f"  delete raw_stats #{r['id']}: {r['player_name']} ({r['club']}, {r['minutes_played']} min)")
    owned_junk = conn.execute(
        f"SELECT COUNT(*) FROM rosters WHERE player_name IN ({','.join('?' * len(junk)) or 'NULL'})",
        [r['player_name'] for r in junk]).fetchone()[0]
    if owned_junk:
        problems.append(f"junk: {owned_junk} roster row(s) reference an Arabic-script name -- not deleting")
        junk = []

    print(f"\nSummary: {len(pickable)} made pickable, {len(twins)} renamed, "
          f"{len(to_add)} added, {len(junk)} junk rows deleted.")
    if problems:
        print("\nNeeds a look (skipped):")
        for p in problems:
            print(f"  - {p}")

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to write.")
        return 0

    try:
        for n in pickable:
            conn.execute("UPDATE players SET draftable=1 WHERE name=?", (n,))
        for new, old in twins:
            rename_everywhere(conn, cols, old, new)  # rosters, shortlists, etc. (not players.name)
            conn.execute("UPDATE players SET name=?, draftable=1 WHERE name=?", (new, old))
        for n, info in to_add:
            cur = conn.execute(
                "INSERT INTO players (name, club, position, whoscored_id, draftable) VALUES (?,?,?,?,1)",
                (n, info['club'], info['primary'], info['whoscored_id']))
            for pos in info['positions']:
                conn.execute("INSERT OR IGNORE INTO player_eligibility (player_id, position, source) "
                             "VALUES (?,?,?)", (cur.lastrowid, pos, 'whoscored_live'))
        for r in junk:
            conn.execute("DELETE FROM raw_stats WHERE id=?", (r['id'],))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\nFAILED, rolled back, nothing written: {e}")
        return 1
    print("\nApplied. Re-run report_missing_from_pool.py to confirm what's left.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
