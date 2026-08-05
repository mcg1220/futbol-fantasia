"""
One-time migration: scope `gameweeks` by (season, gw_number) instead of a
bare-unique gw_number. Previously 2026-27 fixtures silently reused the
2025-26 gameweeks rows since gw_number alone was UNIQUE — this gave every
season its own GW1-38 rows and repoints fixtures.gw_id accordingly.

Run once: python3 migrate_gameweeks_season_scope.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("ALTER TABLE gameweeks RENAME TO gameweeks_old")

c.execute("""
    CREATE TABLE gameweeks (
        id INTEGER PRIMARY KEY,
        gw_number INTEGER NOT NULL,
        season TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'complete',
        is_playoff INTEGER NOT NULL DEFAULT 0,
        UNIQUE(season, gw_number)
    )
""")

# Preserve existing 2025-26 rows with their original ids so fixtures.gw_id
# for that season keeps working with no further changes.
old_rows = c.execute("SELECT id, gw_number, season, status, is_playoff FROM gameweeks_old").fetchall()
for row in old_rows:
    c.execute(
        "INSERT INTO gameweeks (id, gw_number, season, status, is_playoff) VALUES (?,?,?,?,?)",
        row
    )

# New 2026-27 rows, GW1-38. GW34-35 and GW36-37 are fantasy playoffs;
# GW38 is unused by the fantasy league but kept for real-world PL fixtures.
for gw_number in range(1, 39):
    is_playoff = 1 if 34 <= gw_number <= 37 else 0
    c.execute(
        "INSERT INTO gameweeks (gw_number, season, status, is_playoff) VALUES (?,?,?,?)",
        (gw_number, '2026-27', 'pending', is_playoff)
    )

conn.commit()

# Repoint 2026-27 fixtures from the old shared gw_id to the new season-scoped row.
new_gw_ids = {
    row[0]: row[1] for row in
    c.execute("SELECT gw_number, id FROM gameweeks WHERE season='2026-27'").fetchall()
}
old_gw_numbers = {
    row[0]: row[1] for row in
    c.execute("SELECT id, gw_number FROM gameweeks_old").fetchall()
}

fixture_rows = c.execute(
    "SELECT id, gw_id FROM fixtures WHERE season='2026-27'"
).fetchall()

updated = 0
for fixture_id, old_gw_id in fixture_rows:
    gw_number = old_gw_numbers.get(old_gw_id)
    new_gw_id = new_gw_ids.get(gw_number)
    if new_gw_id is None:
        print(f"  WARNING: no new gameweeks row for gw_number={gw_number} (fixture {fixture_id})")
        continue
    c.execute("UPDATE fixtures SET gw_id=? WHERE id=?", (new_gw_id, fixture_id))
    updated += 1

conn.commit()
print(f"Repointed {updated} of {len(fixture_rows)} 2026-27 fixtures to new gameweeks rows.")

c.execute("DROP TABLE gameweeks_old")
conn.commit()

# Sanity checks
total = c.execute("SELECT COUNT(*) FROM gameweeks").fetchone()[0]
by_season = c.execute("SELECT season, COUNT(*) FROM gameweeks GROUP BY season").fetchall()
print(f"gameweeks rows: {total}, by season: {by_season}")

orphans = c.execute("""
    SELECT COUNT(*) FROM fixtures f
    LEFT JOIN gameweeks g ON g.id = f.gw_id
    WHERE g.id IS NULL
""").fetchone()[0]
print(f"Fixtures with no matching gameweeks row: {orphans}")

mismatched = c.execute("""
    SELECT COUNT(*) FROM fixtures f
    JOIN gameweeks g ON g.id = f.gw_id
    WHERE f.season != g.season
""").fetchone()[0]
print(f"Fixtures whose season doesn't match their gameweeks row's season: {mismatched}")

conn.close()
print("Migration complete.")
