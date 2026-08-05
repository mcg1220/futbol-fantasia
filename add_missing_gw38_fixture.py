"""
One-time fix: manually add the missing GW38 2025-26 fixture
(Brighton vs Manchester United, match_id=1903458) that was skipped
during original seeding due to a home/away team-order mismatch.

Run from futbol_fantasia/ folder:
    python add_missing_gw38_fixture.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Confirm it's not already there
existing = c.execute(
    "SELECT id FROM fixtures WHERE match_id = ?", (1903458,)
).fetchone()

if existing:
    print("match_id 1903458 already exists in fixtures — nothing to do.")
else:
    gw_row = c.execute(
        "SELECT id FROM gameweeks WHERE gw_number=? AND season=?", (38, '2025-26')
    ).fetchone()

    if not gw_row:
        print("Could not find GW38 2025-26 gameweek row — aborting.")
    else:
        gw_id = gw_row[0]
        c.execute("""
            INSERT INTO fixtures (gw_id, match_id, home_club, away_club, season)
            VALUES (?, ?, ?, ?, ?)
        """, (gw_id, 1903458, 'Brighton', 'Manchester United', '2025-26'))
        conn.commit()
        print("✅ Added match_id=1903458: Brighton vs Manchester United -> GW38 (2025-26)")

# Verify final count
count = c.execute("""
    SELECT COUNT(*) FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE g.gw_number=38 AND f.season='2025-26'
""").fetchone()[0]
print(f"GW38 2025-26 now has {count} fixtures (should be 10).")

conn.close()
