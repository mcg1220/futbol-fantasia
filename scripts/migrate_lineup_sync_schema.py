"""
One-time migration: schema prep for the FotMob confirmed-lineup sync.

1. fixtures.fotmob_id -- FotMob's own match id, captured once during the
   fixture schedule sync so the lineup sync can look up a specific match's
   details directly (matchDetails?matchId=<fotmob_id>) instead of re-
   fetching and re-matching the whole season every time it polls.

2. player_start_status.updated_by -- currently NOT NULL with a FK to
   managers(id), since until now this was only ever set by a manager
   clicking the tri-state icon. The lineup sync sets 'starting' for a
   confirmed real-world starter automatically, with no manager behind the
   action -- attributing that to a real manager's id would misrepresent
   who actually set it (this column isn't shown in the UI today, but the
   data itself should stay honest). SQLite can't ALTER COLUMN to relax a
   NOT NULL/FK constraint in place, so this rebuilds the table: same
   columns and the same UNIQUE(player_name, gw, season) constraint, just
   with updated_by made nullable. NULL means "set automatically by the
   lineup sync, not by a manager."

Run once: python3 migrate_lineup_sync_schema.py
"""
import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

cols = [r[1] for r in c.execute("PRAGMA table_info(fixtures)").fetchall()]
if 'fotmob_id' not in cols:
    c.execute("ALTER TABLE fixtures ADD COLUMN fotmob_id INTEGER")
    print("Added fixtures.fotmob_id")
else:
    print("fixtures.fotmob_id already exists")

updated_by_col = next(
    (r for r in c.execute("PRAGMA table_info(player_start_status)").fetchall() if r[1] == 'updated_by'),
    None
)
if updated_by_col and updated_by_col[3] == 1:  # notnull flag
    print("Rebuilding player_start_status with updated_by nullable...")
    c.execute("""
        CREATE TABLE player_start_status_new (
            id INTEGER PRIMARY KEY,
            player_name TEXT NOT NULL,
            gw INTEGER NOT NULL,
            season TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            UNIQUE(player_name, gw, season),
            FOREIGN KEY (updated_by) REFERENCES managers(id)
        )
    """)
    c.execute("""
        INSERT INTO player_start_status_new (id, player_name, gw, season, status, updated_by, updated_at)
        SELECT id, player_name, gw, season, status, updated_by, updated_at FROM player_start_status
    """)
    c.execute("DROP TABLE player_start_status")
    c.execute("ALTER TABLE player_start_status_new RENAME TO player_start_status")
    print("player_start_status.updated_by is now nullable")
else:
    print("player_start_status.updated_by is already nullable (or table doesn't exist yet)")

conn.commit()
print("Done.")
conn.close()
