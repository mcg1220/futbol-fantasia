"""
Diagnostic: check fixture counts per GW for 2025-26, and locate specific
match IDs the user flagged as misassigned.

Run from futbol_fantasia/ folder:
    python diagnose_fixtures.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("FIXTURE COUNTS PER GW (2025-26)")
print("=" * 60)
rows = c.execute("""
    SELECT g.gw_number, COUNT(*) as cnt
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE f.season = '2025-26'
    GROUP BY g.gw_number
    ORDER BY g.gw_number
""").fetchall()

for gw_number, cnt in rows:
    flag = "  ⚠️  NOT 10" if cnt != 10 else ""
    print(f"  GW{gw_number}: {cnt} fixtures{flag}")

print("\n" + "=" * 60)
print("SPECIFIC MATCH ID LOOKUP")
print("=" * 60)
flagged_ids = [1903384, 1903389, 1903466]
for mid in flagged_ids:
    row = c.execute("""
        SELECT f.match_id, f.home_club, f.away_club, g.gw_number, f.season
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.match_id = ?
    """, (mid,)).fetchone()
    if row:
        print(f"  match_id={row[0]}: {row[1]} vs {row[2]} -> currently GW{row[3]} ({row[4]})")
    else:
        print(f"  match_id={mid}: NOT FOUND in fixtures table at all")

print("\n" + "=" * 60)
print("ANY DUPLICATE MATCH_IDS (same match assigned twice)?")
print("=" * 60)
dupes = c.execute("""
    SELECT match_id, COUNT(*) as cnt
    FROM fixtures
    WHERE season = '2025-26'
    GROUP BY match_id
    HAVING cnt > 1
""").fetchall()
if dupes:
    for match_id, cnt in dupes:
        print(f"  match_id={match_id} appears {cnt} times")
else:
    print("  None found.")

conn.close()
