"""
One-time migration: add audit_log table — a unified history of every
create/update/delete action across the app, excluding Locker Room,
Transfer Room, and the Scraper (which already have their own logs).

Run once: python3 migrate_audit_log_table.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER,
        actor_name TEXT,
        entity_type TEXT NOT NULL,
        action TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail_json TEXT,
        created_at TEXT NOT NULL
    )
""")

conn.commit()
print("audit_log table created.")
conn.close()
