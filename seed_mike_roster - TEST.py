"""
One-time script: seed Mike's team page with his GW33 2025-26 roster
so we can preview the team page with real players.

Run from futbol_fantasia/ folder:
    python seed_mike_roster.py

Safe to re-run — clears Mike's GW1 2026-27 roster first.
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Find Mike's manager_id
mike = c.execute("SELECT id FROM managers WHERE name='Mike'").fetchone()
if not mike:
    print("Mike not found in managers table.")
    conn.close()
    exit()
mike_id = mike[0]
print(f"Mike's manager_id: {mike_id}")

# Find his last GW with roster data in 2025-26
last_gw = c.execute("""
    SELECT MAX(r.gw_start)
    FROM rosters r
    WHERE r.manager_id = ?
""", (mike_id,)).fetchone()[0]

if not last_gw:
    print("No existing roster found for Mike.")
    conn.close()
    exit()
print(f"Using roster from GW{last_gw}")

# Pull his roster from that GW
old_roster = c.execute("""
    SELECT player_name, slot_type, position_slot
    FROM rosters
    WHERE manager_id = ? AND gw_start = ?
""", (mike_id, last_gw)).fetchall()

print(f"Found {len(old_roster)} players")

# Clear any existing GW1 2026-27 roster for Mike
c.execute("""
    DELETE FROM rosters
    WHERE manager_id = ? AND gw_start = 1 AND gw_end = 1
""", (mike_id,))

# Insert as GW1 2026-27
for player_name, slot_type, position_slot in old_roster:
    c.execute("""
        INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
        VALUES (?, ?, ?, ?, 1, 1)
    """, (mike_id, player_name, slot_type, position_slot))
    print(f"  {slot_type:8} {position_slot:6} {player_name}")

conn.commit()
conn.close()
print(f"\nDone. Visit /team/{mike_id} to preview.")
