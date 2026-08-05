"""
One-time migration: add `scraper_runs` (persistent scraper audit log) and
`transactions` (roster add/drop/waiver transaction log).

Run once: python3 migrate_audit_tables.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS scraper_runs (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        gw_start INTEGER NOT NULL,
        gw_end INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL,
        total_fixtures INTEGER NOT NULL DEFAULT 0,
        perfect_count INTEGER NOT NULL DEFAULT 0,
        discrepancy_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        added_player TEXT,
        dropped_player TEXT,
        source TEXT NOT NULL,
        gw INTEGER,
        season TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("scraper_runs and transactions tables created.")
conn.close()
