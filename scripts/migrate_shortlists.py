"""
One-time migration: add the shortlists table for the per-manager player
shortlist feature (Main Draft + Add/Drop watchlist).

Run once: python3 migrate_shortlists.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS shortlists (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        added_at TEXT NOT NULL,
        UNIQUE(manager_id, player_name),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("shortlists table created.")
conn.close()
