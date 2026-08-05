"""
One-time migration: add transfer_journalists table for the Transfer Room tab,
seeded with the initial trusted journalist allowlist.

Run once: python3 migrate_transfer_journalists_table.py
"""

import sqlite3
from datetime import datetime
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_journalists (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        x_handle TEXT NOT NULL,
        notes TEXT,
        added_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
""")

seed = [
    ("Fabrizio Romano", "FabrizioRomano", "Direct source relationships across European clubs, distinct from aggregator/rumor accounts."),
    ("David Ornstein", "David_Ornstein", "The Athletic's chief football correspondent; independently verified direct-source track record."),
]
now = datetime.now().isoformat()
for name, handle, notes in seed:
    c.execute("""
        INSERT INTO transfer_journalists (name, x_handle, notes, added_at, active)
        SELECT ?, ?, ?, ?, 1
        WHERE NOT EXISTS (SELECT 1 FROM transfer_journalists WHERE x_handle = ?)
    """, (name, handle, notes, now, handle))

conn.commit()
print("transfer_journalists table created and seeded with Romano + Ornstein.")
conn.close()
