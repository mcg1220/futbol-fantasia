"""
Deeper diagnostic: confirm where match_id 1903466 (Man City vs Crystal Palace)
actually belongs by checking if both teams are free in GW31.

Run from futbol_fantasia/ folder:
    python diagnose_gw31_36.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("ALL FIXTURES CURRENTLY IN GW31 (2025-26)")
print("=" * 60)
rows = c.execute("""
    SELECT f.match_id, f.home_club, f.away_club
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE g.gw_number = 31 AND f.season = '2025-26'
    ORDER BY f.match_id
""").fetchall()
for r in rows:
    print(f"  match_id={r[0]}: {r[1]} vs {r[2]}")

print("\n" + "=" * 60)
print("ALL FIXTURES CURRENTLY IN GW36 (2025-26)")
print("=" * 60)
rows = c.execute("""
    SELECT f.match_id, f.home_club, f.away_club
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE g.gw_number = 36 AND f.season = '2025-26'
    ORDER BY f.match_id
""").fetchall()
for r in rows:
    print(f"  match_id={r[0]}: {r[1]} vs {r[2]}")

print("\n" + "=" * 60)
print("EVERY MANCHESTER CITY FIXTURE, GW BY GW (should be one per GW, 1-38)")
print("=" * 60)
rows = c.execute("""
    SELECT g.gw_number, f.match_id, f.home_club, f.away_club
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE f.season = '2025-26'
      AND (f.home_club = 'Manchester City' OR f.away_club = 'Manchester City')
    ORDER BY g.gw_number
""").fetchall()
seen_gws = set()
for r in rows:
    print(f"  GW{r[0]}: match_id={r[1]}: {r[2]} vs {r[3]}")
    seen_gws.add(r[0])
missing = [g for g in range(1, 39) if g not in seen_gws]
print(f"  Man City MISSING from GWs: {missing}")

print("\n" + "=" * 60)
print("EVERY CRYSTAL PALACE FIXTURE, GW BY GW")
print("=" * 60)
rows = c.execute("""
    SELECT g.gw_number, f.match_id, f.home_club, f.away_club
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE f.season = '2025-26'
      AND (f.home_club = 'Crystal Palace' OR f.away_club = 'Crystal Palace')
    ORDER BY g.gw_number
""").fetchall()
seen_gws = set()
for r in rows:
    print(f"  GW{r[0]}: match_id={r[1]}: {r[2]} vs {r[3]}")
    seen_gws.add(r[0])
missing = [g for g in range(1, 39) if g not in seen_gws]
print(f"  Crystal Palace MISSING from GWs: {missing}")

conn.close()
