"""
One-time migration: add season column to fixtures table and populate it.

Root cause: gameweeks table has UNIQUE(gw_number) so can't hold two seasons.
Fix: tag each fixture directly with its season via match_id range.
  - 2025-26 fixtures: match_id < 1983000
  - 2026-27 fixtures: match_id >= 1983000
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Step 1 — Add season column to fixtures if not already there
cols = [row[1] for row in c.execute("PRAGMA table_info(fixtures)").fetchall()]
if 'season' not in cols:
    c.execute("ALTER TABLE fixtures ADD COLUMN season TEXT")
    print("Added 'season' column to fixtures table.")
else:
    print("'season' column already exists.")

# Step 2 — Tag fixtures by season based on match_id range
c.execute("UPDATE fixtures SET season='2025-26' WHERE match_id < 1983000")
c.execute("UPDATE fixtures SET season='2026-27' WHERE match_id >= 1983000")
conn.commit()

# Verify
counts = c.execute(
    "SELECT season, COUNT(*) FROM fixtures GROUP BY season"
).fetchall()
print("\nFixture counts by season:")
for season, count in counts:
    print(f"  {season}: {count} fixtures")

conn.close()
print("\nDone. Restart app.py and check /gameweek.")
