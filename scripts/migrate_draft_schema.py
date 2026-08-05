"""
One-time migration: add draft tables (draft_state, draft_order, draft_picks,
draft_comments) and wipe existing `rosters` rows so the 2026-27 draft starts
from a clean slate. The wiped rows were proof-of-concept validation data
seeded against 2025-26 gameweeks (GW1/5/12/19/26/33) during scoring-engine
testing, not real 2026-27 rosters.

Run once: python3 migrate_draft_schema.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_state (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'not_started',
        spin_count INTEGER NOT NULL DEFAULT 0,
        current_pick_number INTEGER NOT NULL DEFAULT 0,
        rounds INTEGER NOT NULL DEFAULT 15,
        teams_count INTEGER NOT NULL DEFAULT 8
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_order (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        pick_slot INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        UNIQUE(season, pick_slot),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_picks (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        overall_pick INTEGER NOT NULL,
        round INTEGER NOT NULL,
        pick_in_round INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        slot_type TEXT NOT NULL,
        position_slot TEXT,
        picked_at TEXT NOT NULL,
        UNIQUE(season, overall_pick),
        UNIQUE(season, player_name),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_comments (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        comment TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
""")

conn.commit()

wiped = c.execute("SELECT COUNT(*) FROM rosters").fetchone()[0]
c.execute("DELETE FROM rosters")
conn.commit()

print(f"Draft tables created. Wiped {wiped} legacy roster rows.")
conn.close()
