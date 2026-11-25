"""
Read-only report: every player with scraped stats this season who is NOT
pickable in Add/Drop (or the draft / transfer pools), and why.

A player is pickable only if they have a `players` row whose club is set,
isn't relegated, and draftable = 1. The scrape upload only writes raw_stats;
it never creates or updates a `players` row. So anyone who arrived after the
pre-season Fantrax import, or who was soft-excluded pre-season as a "fringe"
player and has since started playing, silently never becomes pickable.

Buckets:
  SPELLING TWIN    no players row under the scraped (WhoScored) name, but a
                   similar name exists -> duplicate-player bug, fix with the
                   fix_koumas_duplicate.py approach (WhoScored spelling wins)
  NO PLAYERS ROW   no players row and no similar name -> needs adding
  NOT DRAFTABLE    players row with draftable = 0 (soft-excluded pre-season)
  NO CLUB          players row with a blank club
  RELEGATED CLUB   players row still on a relegated club, but playing for a
                   current PL club per the scraped stats -> stale club
Plus, informational: pickable players whose listed club differs from the
club they're actually playing for.

Writes nothing. Run in Render Shell:  python3 scripts/report_missing_from_pool.py
"""
import argparse
import difflib
import sqlite3
import sys
import unicodedata

sys.path.insert(0, 'scripts')
from init_db import DB_PATH

SEASON = '2026-27'
RELEGATED_CLUBS = ('Wolves', 'West Ham', 'Burnley')  # keep in sync with app.py


def normalize(name):
    d = unicodedata.normalize('NFKD', name or '')
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB_PATH)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    has_draftable = 'draftable' in pcols

    # Everyone with scraped appearances this season, with their most recent club.
    scraped = conn.execute("""
        SELECT rs.player_name AS name,
               COUNT(DISTINCT rs.match_id) AS apps,
               SUM(COALESCE(rs.minutes_played, 0)) AS mins,
               SUM(CASE WHEN rs.minutes_played > 0 THEN 1 ELSE 0 END) AS played,
               MIN(g.gw_number) AS first_gw, MAX(g.gw_number) AS last_gw
        FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season = ? AND rs.external = 0
        GROUP BY rs.player_name
    """, (SEASON,)).fetchall()
    latest_club = {r['player_name']: r['club'] for r in conn.execute("""
        SELECT rs.player_name, rs.club FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season = ? AND rs.external = 0
        ORDER BY g.gw_number
    """, (SEASON,))}  # later rows overwrite earlier ones -> latest club

    players = {r['name']: dict(r) for r in conn.execute(
        f"SELECT name, club, position{', draftable' if has_draftable else ''} FROM players")}
    owned = {r[0]: r[1] for r in conn.execute("""
        SELECT r.player_name, m.name FROM rosters r JOIN managers m ON m.id = r.manager_id
        WHERE r.gw_end IS NULL""")}
    scraped_names = {r['name'] for r in scraped}

    by_norm, by_surname = {}, {}
    for n in players:
        by_norm.setdefault(normalize(n), []).append(n)
        parts = normalize(n).split()
        if parts:
            by_surname.setdefault(parts[-1], []).append(n)

    buckets = {k: [] for k in ('SPELLING TWIN', 'NO PLAYERS ROW', 'NOT DRAFTABLE',
                               'NO CLUB', 'RELEGATED CLUB')}
    wrong_club = []
    for r in scraped:
        name = r['name']
        club_now = latest_club.get(name)
        info = dict(name=name, club_now=club_now, apps=r['apps'], played=r['played'],
                    mins=r['mins'], gws=f"GW{r['first_gw']}-{r['last_gw']}", owner=owned.get(name))
        p = players.get(name)
        if p is None:
            # Similar names that are NOT themselves scraped under that spelling.
            cands = set(by_norm.get(normalize(name), []))
            parts = normalize(name).split()
            if parts:
                cands |= set(by_surname.get(parts[-1], []))
            cands |= set(difflib.get_close_matches(name, list(players), n=3, cutoff=0.8))
            cands = sorted(c for c in cands if c not in scraped_names)
            info['note'] = ', '.join(
                f"{c} ({players[c]['club'] or 'no club'}"
                f"{', draftable=0' if has_draftable and players[c].get('draftable') == 0 else ''}"
                f"{', owned by ' + owned[c] if c in owned else ''})" for c in cands)
            buckets['SPELLING TWIN' if cands else 'NO PLAYERS ROW'].append(info)
            continue
        info['note'] = f"listed as {p['club'] or 'no club'} / {p['position'] or '?'}"
        if has_draftable and p.get('draftable') == 0:
            buckets['NOT DRAFTABLE'].append(info)
        elif not p['club']:
            buckets['NO CLUB'].append(info)
        elif p['club'] in RELEGATED_CLUBS:
            buckets['RELEGATED CLUB'].append(info)
        elif club_now and p['club'] != club_now:
            wrong_club.append(info)

    def show(title, rows):
        print(f"\n=== {title}: {len(rows)} ===")
        for i in sorted(rows, key=lambda x: -x['mins']):
            own = f"  [OWNED by {i['owner']}]" if i['owner'] else ''
            print(f"  {i['name']:28} {str(i['club_now']):16} {i['played']:>2} played/{i['apps']:>2} in squad "
                  f"{i['mins']:>5} min  {i['gws']:9}  {i['note']}{own}")

    total = sum(len(v) for v in buckets.values())
    print(f"Season {SEASON}: {len(scraped)} players with scraped stats; "
          f"{total} of them are NOT pickable in Add/Drop.")
    print(f"(has draftable column: {has_draftable})")
    for k, v in buckets.items():
        show(k, v)
    show("PICKABLE BUT LISTED UNDER A DIFFERENT CLUB (informational)", wrong_club)
    played_any = [i for v in buckets.values() for i in v if i['mins'] > 0]
    print(f"\nOf the {total} not pickable, {len(played_any)} have actually played minutes this season.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
