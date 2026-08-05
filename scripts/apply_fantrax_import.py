"""
Step 2 of the Fantrax import: apply confirmed changes to the database.

Covers everything the user has already signed off on:
- Merge 2 duplicate DB player rows (blank-club dupes with no other data)
- Update players.club for 81 confirmed transfers
- Insert 36 confirmed new players (excludes zero-projection players + Aidan Dausch)
- Upsert player_projections for same-club + transfers + the 36 new players
- Fill in fixtures.match_date/kickoff_time for GW1-5 from the 5 schedule CSVs

Deliberately excludes the 36 ambiguous players pending the user's review.

Run: python3 apply_fantrax_import.py
"""

import csv
import json
import re
import sqlite3
import unicodedata
from init_db import DB_PATH

CSV_DIR = "/Users/michaelgarcia/futbol_fantasia/fantrax schedule and projections"
PROJECTIONS_CSV = f"{CSV_DIR}/Fantrax-Players-Fantasy Futbol - Projected Stats.csv"
SCHEDULE_CSVS = [f"{CSV_DIR}/Fantrax-Players-Fantasy Futbol - Schedule ({i} of 5).csv" for i in range(1, 6)]

TEAM_MAP = {
    'ARS': 'Arsenal', 'AVL': 'Aston Villa', 'BHA': 'Brighton', 'BOU': 'Bournemouth',
    'BRF': 'Brentford', 'CHE': 'Chelsea', 'COV': 'Coventry', 'CRY': 'Crystal Palace',
    'EVE': 'Everton', 'FUL': 'Fulham', 'HUL': 'Hull', 'IPS': 'Ipswich', 'LEE': 'Leeds',
    'LIV': 'Liverpool', 'MCI': 'Manchester City', 'MUN': 'Manchester United',
    'NEW': 'Newcastle', 'NOT': 'Nottingham Forest', 'SUN': 'Sunderland', 'TOT': 'Tottenham',
}
POSITION_MAP = {'G': 'GK', 'D': 'DEF', 'M': 'MID', 'F': 'FW'}
SEASON = '2026-27'

EXCLUDED_NEW_PLAYERS = {'Aidan Dausch'}

DUPLICATE_MERGES = [
    # (blank-club id to delete, canonical id to keep)
    (206, 205),  # Filip Jørgensen -> Filip Jörgensen (Chelsea)
    (997, 562),  # Luca Williams-Barnett -> Lucá Williams-Barnett (Tottenham)
]


def merge_duplicates(conn):
    for dupe_id, keep_id in DUPLICATE_MERGES:
        dupe = conn.execute("SELECT name, club FROM players WHERE id = ?", (dupe_id,)).fetchone()
        keep = conn.execute("SELECT name, club FROM players WHERE id = ?", (keep_id,)).fetchone()
        if not dupe or not keep:
            print(f"  SKIP merge {dupe_id}->{keep_id}: one of the rows is missing")
            continue
        conn.execute("DELETE FROM player_eligibility WHERE player_id = ?", (dupe_id,))
        conn.execute("DELETE FROM players WHERE id = ?", (dupe_id,))
        print(f"  Merged '{dupe[0]}' (id {dupe_id}) into '{keep[0]}' (id {keep_id})")


def apply_transfers(conn, transfers):
    for t in transfers:
        conn.execute("UPDATE players SET club = ? WHERE name = ?", (t['fantrax_club'], t['db_name']))
    print(f"  Updated club for {len(transfers)} players")


def insert_new_players(conn, new_players):
    inserted = 0
    for p in new_players:
        if p['fantrax_name'] in EXCLUDED_NEW_PLAYERS or float(p['fpts']) == 0:
            continue
        primary_pos = p['fantrax_positions'][0]
        cur = conn.execute(
            "INSERT INTO players (name, club, position) VALUES (?, ?, ?)",
            (p['fantrax_name'], p['fantrax_club'], primary_pos)
        )
        player_id = cur.lastrowid
        for pos in p['fantrax_positions']:
            conn.execute(
                "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
                (player_id, pos, 'fantrax_import_placeholder')
            )
        inserted += 1
    print(f"  Inserted {inserted} new players (of {len(new_players)} candidates)")
    return inserted


def upsert_projections(conn, all_rows, new_players):
    excluded_names = EXCLUDED_NEW_PLAYERS | {p['fantrax_name'] for p in new_players
                                              if float(p['fpts']) == 0}
    count = 0
    for row in all_rows:
        name = row.get('db_name') or row['fantrax_name']
        if row['fantrax_name'] in excluded_names:
            continue
        conn.execute("""
            INSERT INTO player_projections (season, player_name, proj_total, proj_avg)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(season, player_name) DO UPDATE SET
                proj_total = excluded.proj_total, proj_avg = excluded.proj_avg
        """, (SEASON, name, float(row['fpts']), float(row['fpg'])))
        count += 1
    print(f"  Upserted {count} projection rows for season {SEASON}")


MANUAL_CHAR_MAP = str.maketrans({
    'Ø': 'O', 'ø': 'o', 'Đ': 'D', 'đ': 'd', 'Þ': 'Th', 'þ': 'th',
    'Æ': 'Ae', 'æ': 'ae', 'Œ': 'Oe', 'œ': 'oe', 'ß': 'ss', 'Ł': 'L', 'ł': 'l',
})


def normalize_name(name):
    name = name.translate(MANUAL_CHAR_MAP)
    name = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    name = name.replace('-', ' ').replace("'", '')
    return ' '.join(name.lower().split())


CELL_RE = re.compile(r'^(@)?([A-Z]+)<br/>(\w+) (\d{1,2}):(\d{2})(AM|PM)$')


def parse_cell(cell, month, day):
    m = CELL_RE.match(cell.strip())
    if not m:
        return None
    away_flag, club_code, weekday, hh, mm, ampm = m.groups()
    hh = int(hh)
    if ampm == 'PM' and hh != 12:
        hh += 12
    if ampm == 'AM' and hh == 12:
        hh = 0
    return {
        'club': TEAM_MAP.get(club_code, club_code),
        'is_away': bool(away_flag),
        'date': f"2026-{month:02d}-{day:02d}",
        'time': f"{hh:02d}:{mm}",
    }


def update_fixtures(conn):
    gw_map = {i + 1: gw_id for i, gw_id in enumerate(range(39, 44))}  # gw1->39 ... gw5->43

    for gw_num, csv_path in zip(range(1, 6), SCHEDULE_CSVS):
        gw_id = gw_map[gw_num]
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            date_cols = [c for c in reader.fieldnames if re.match(r'^\w{3} \d{1,2}/\d{1,2}$', c)]

            # club -> {date, time, is_away} (dedupe: every player at a club shares the same fixture)
            club_fixture = {}
            for row in reader:
                for col in date_cols:
                    cell = row.get(col, '')
                    if not cell.strip():
                        continue
                    weekday, mmdd = col.split(' ')
                    month, day = (int(x) for x in mmdd.split('/'))
                    parsed = parse_cell(cell, month, day)
                    if parsed and parsed['club'] not in club_fixture:
                        club_fixture[parsed['club']] = parsed

        fixtures = conn.execute(
            "SELECT id, home_club, away_club FROM fixtures WHERE gw_id = ? AND season = ?",
            (gw_id, SEASON)
        ).fetchall()

        updated = 0
        for fid, home_club, away_club in fixtures:
            info = club_fixture.get(home_club) or club_fixture.get(away_club)
            if not info:
                print(f"  WARN: no schedule data found for {home_club} vs {away_club} (GW{gw_num})")
                continue
            conn.execute(
                "UPDATE fixtures SET match_date = ?, kickoff_time = ? WHERE id = ?",
                (info['date'], info['time'], fid)
            )
            updated += 1
        print(f"  GW{gw_num}: updated {updated}/{len(fixtures)} fixtures")


def main():
    with open('/tmp/fantrax_diff.json') as f:
        diff = json.load(f)

    conn = sqlite3.connect(DB_PATH)

    print("1. Merging duplicate players...")
    merge_duplicates(conn)

    print("2. Applying confirmed club transfers...")
    apply_transfers(conn, diff['transfers'])

    print("3. Inserting confirmed new players...")
    insert_new_players(conn, diff['new_players'])

    print("4. Upserting projections (same-club + transfers + new players)...")
    db_by_norm = {}
    for name, in conn.execute("SELECT name FROM players"):
        db_by_norm[normalize_name(name)] = name

    same_club_rows = []
    with open(PROJECTIONS_CSV) as f:
        reader = csv.DictReader(f)
        by_norm_transfers = {normalize_name(t['fantrax_name']) for t in diff['transfers']}
        by_norm_ambiguous = {normalize_name(a['fantrax_name']) for a in diff['ambiguous']}
        by_norm_new = {normalize_name(p['fantrax_name']) for p in diff['new_players']}
        for row in reader:
            norm = normalize_name(row['Player'])
            if norm in by_norm_transfers or norm in by_norm_ambiguous or norm in by_norm_new:
                continue
            db_name = db_by_norm.get(norm, row['Player'])
            same_club_rows.append({
                'fantrax_name': row['Player'], 'db_name': db_name,
                'fpts': row['FPts'], 'fpg': row['FP/G'],
            })

    upsert_projections(conn, same_club_rows + diff['transfers'] + diff['new_players'], diff['new_players'])

    print("5. Updating fixtures for GW1-5...")
    update_fixtures(conn)

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == '__main__':
    main()
