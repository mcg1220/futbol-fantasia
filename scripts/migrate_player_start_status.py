"""
One-time migration: add the player_start_status table -- lets a roster
owner manually record whether a player actually started for their
real-world club in a given gameweek (not scraped, since nothing feeds
this automatically). Global per (player_name, gw, season), not per
manager, since a player is only ever on one active roster at a time.
No row = unknown/not filled in.

Run once: python3 migrate_player_start_status.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS player_start_status (
        id INTEGER PRIMARY KEY,
        player_name TEXT NOT NULL,
        gw INTEGER NOT NULL,
        season TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(player_name, gw, season),
        FOREIGN KEY (updated_by) REFERENCES managers(id)
    )
""")

conn.commit()
print("player_start_status table created.")
conn.close()
