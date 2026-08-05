"""
One-time corrective migration: fix 3 misassigned 2025-26 fixtures.

Confirmed via diagnostics:
  - match_id 1903384 (Bournemouth vs Leeds)      GW33 -> GW34
  - match_id 1903389 (Burnley vs Manchester City) GW33 -> GW34
  - match_id 1903466 (Man City vs Crystal Palace)  GW36 -> GW31

After this: GW31/33/34/36 should all read exactly 10. GW38 stays at 9
(Brighton vs Man Utd was never resolved during original seeding — known,
accepted gap; that match doesn't exist in the fixtures table at all).

Run from futbol_fantasia/ folder:
    python fix_gw_assignments.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

def get_gw_id(gw_number, season='2025-26'):
    row = c.execute(
        "SELECT id FROM gameweeks WHERE gw_number=? AND season=?",
        (gw_number, season)
    ).fetchone()
    return row[0] if row else None

moves = [
    (1903384, 34, "Bournemouth vs Leeds"),
    (1903389, 34, "Burnley vs Manchester City"),
    (1903466, 31, "Manchester City vs Crystal Palace"),
]

print("Applying corrections...\n")
for match_id, target_gw, label in moves:
    target_gw_id = get_gw_id(target_gw)
    if not target_gw_id:
        print(f"  ⚠️  Could not find GW{target_gw} gameweek row — skipping {label}")
        continue
    c.execute("UPDATE fixtures SET gw_id=? WHERE match_id=?", (target_gw_id, match_id))
    print(f"  ✅ match_id={match_id} ({label}) -> GW{target_gw}")

conn.commit()

print("\n" + "=" * 60)
print("VERIFICATION — fixture counts per GW after fix")
print("=" * 60)
rows = c.execute("""
    SELECT g.gw_number, COUNT(*) as cnt
    FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE f.season = '2025-26'
    GROUP BY g.gw_number
    ORDER BY g.gw_number
""").fetchall()

all_good = True
for gw_number, cnt in rows:
    expected = 9 if gw_number == 38 else 10  # GW38 known permanent gap
    flag = "" if cnt == expected else "  ⚠️  UNEXPECTED"
    if flag:
        all_good = False
    print(f"  GW{gw_number}: {cnt} fixtures{flag}")

print()
if all_good:
    print("✅ All GWs now match expected counts (GW38 = 9, known gap; all others = 10).")
else:
    print("⚠️  Some GWs still don't match expected counts — needs further investigation.")

conn.close()
