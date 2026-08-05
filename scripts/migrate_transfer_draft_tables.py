"""
One-time migration: add transfer_pool, transfer_drafts, transfer_draft_order,
transfer_draft_picks tables for the Summer/Winter Transfer Draft feature.

Run once: python3 migrate_transfer_draft_tables.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_pool (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        player_name TEXT NOT NULL,
        previous_club TEXT,
        added_by TEXT,
        added_at TEXT NOT NULL,
        UNIQUE(season, draft_type, player_name)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_drafts (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'not_started',
        round INTEGER NOT NULL DEFAULT 1,
        current_pick_number INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        UNIQUE(season, draft_type)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_draft_order (
        id INTEGER PRIMARY KEY,
        transfer_draft_id INTEGER NOT NULL,
        round INTEGER NOT NULL,
        position INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        UNIQUE(transfer_draft_id, round, position),
        FOREIGN KEY (transfer_draft_id) REFERENCES transfer_drafts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_draft_picks (
        id INTEGER PRIMARY KEY,
        transfer_draft_id INTEGER NOT NULL,
        round INTEGER NOT NULL,
        overall_pick INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        player_name TEXT,
        dropped_player TEXT,
        is_pass INTEGER NOT NULL DEFAULT 0,
        picked_at TEXT NOT NULL,
        FOREIGN KEY (transfer_draft_id) REFERENCES transfer_drafts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("transfer_pool, transfer_drafts, transfer_draft_order, transfer_draft_picks tables created.")
conn.close()
