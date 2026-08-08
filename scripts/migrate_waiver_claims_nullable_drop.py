"""
One-time migration: allow waiver_claims.drop_player to be NULL, for claims
that add a player without dropping anyone (a manager with an open roster
spot — e.g. someone moved to a previously-empty IR slot). SQLite can't
ALTER a column's NOT NULL constraint directly, so this recreates the table.

Run once: python3 migrate_waiver_claims_nullable_drop.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("ALTER TABLE waiver_claims RENAME TO waiver_claims_old")

c.execute("""
    CREATE TABLE waiver_claims (
        id INTEGER PRIMARY KEY,
        window_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        add_player TEXT NOT NULL,
        drop_player TEXT,
        priority INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        fail_reason TEXT,
        sequence_number INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(window_id, manager_id, priority),
        FOREIGN KEY (window_id) REFERENCES waiver_windows(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    INSERT INTO waiver_claims (id, window_id, manager_id, add_player, drop_player,
                                priority, status, fail_reason, sequence_number, created_at)
    SELECT id, window_id, manager_id, add_player, drop_player,
           priority, status, fail_reason, sequence_number, created_at
    FROM waiver_claims_old
""")

moved = c.execute("SELECT COUNT(*) FROM waiver_claims").fetchone()[0]
c.execute("DROP TABLE waiver_claims_old")

conn.commit()
print(f"waiver_claims.drop_player is now nullable. Preserved {moved} existing claims.")
conn.close()
