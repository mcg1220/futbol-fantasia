"""
One-time import: populate players table AND player_eligibility from a
WhoScored position export.

Discovered that the `players` table was completely empty (0 rows), which is
why the eligibility import initially matched nothing. This version creates
each player (backfilling club from raw_stats where available) and their
eligibility rows in one pass.

Converts D/M/F/G -> DEF/MID/FW/GK.

Run from futbol_fantasia/ folder:
    python import_player_eligibility.py
"""

import sqlite3
import os

DB_PATH  = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
TSV_PATH = os.path.join(os.path.dirname(__file__), 'data', 'whoscored_eligibility_2025-26.tsv')

POS_MAP = {'D': 'DEF', 'M': 'MID', 'F': 'FW', 'G': 'GK'}

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Clear any previous import from this source before re-running
c.execute("DELETE FROM player_eligibility WHERE source = 'whoscored_2025-26'")

# Build name -> club lookup from raw_stats (most recent appearance)
club_rows = c.execute("""
    SELECT player_name, club FROM raw_stats
    GROUP BY player_name HAVING MAX(gw_number)
""").fetchall()
club_map = {name: club for name, club in club_rows}

created_players = 0
existing_players = 0
total_positions_inserted = 0
no_club_found = []

with open(TSV_PATH, encoding='utf-8') as f:
    for line in f:
        line = line.rstrip('\n')
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) != 2:
            continue
        name, positions_raw = parts[0].strip(), parts[1].strip()

        positions = [p.strip() for p in positions_raw.split(',')]
        db_positions = [POS_MAP[p] for p in positions if p in POS_MAP]
        if not db_positions:
            continue
        primary_position = db_positions[0]  # first listed position as primary

        # Does this player already exist?
        row = c.execute("SELECT id FROM players WHERE name=?", (name,)).fetchone()
        if row:
            player_id = row[0]
            existing_players += 1
        else:
            club = club_map.get(name, '')
            if not club:
                no_club_found.append(name)
            c.execute(
                "INSERT INTO players (name, club, position) VALUES (?, ?, ?)",
                (name, club, primary_position)
            )
            player_id = c.lastrowid
            created_players += 1

        for db_pos in db_positions:
            c.execute("""
                INSERT OR IGNORE INTO player_eligibility (player_id, position, source)
                VALUES (?, ?, 'whoscored_2025-26')
            """, (player_id, db_pos))
            total_positions_inserted += 1

conn.commit()

print(f"Created {created_players} new players.")
print(f"Found {existing_players} already-existing players.")
print(f"Inserted {total_positions_inserted} eligibility rows.")
print(f"\nPlayers created WITHOUT a club match from raw_stats: {len(no_club_found)}")
if no_club_found:
    print("(These didn't appear in any scraped match — likely bench players who")
    print(" never featured, or players who transferred out before we started scraping.)")
    for name in no_club_found[:30]:
        print(f"  - {name}")
    if len(no_club_found) > 30:
        print(f"  ... and {len(no_club_found) - 30} more")

conn.close()
