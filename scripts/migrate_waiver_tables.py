"""
One-time migration: add waiver_order, waiver_windows, waiver_claims tables
for the Waiver Wire feature (merged into the Player Add/Drop page).

Run once: python3 migrate_waiver_tables.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_order (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        manager_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        UNIQUE(season, manager_id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_windows (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        window_number INTEGER NOT NULL,
        gw INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        UNIQUE(season, window_number)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_claims (
        id INTEGER PRIMARY KEY,
        window_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        add_player TEXT NOT NULL,
        drop_player TEXT NOT NULL,
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

conn.commit()
print("waiver_order, waiver_windows, waiver_claims tables created.")
conn.close()
