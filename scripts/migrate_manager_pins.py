"""
One-time migration: add managers.pin_hash for the session-based login
feature — NULL means that manager hasn't claimed their account/set a PIN
yet.

Run once: python3 migrate_manager_pins.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("ALTER TABLE managers ADD COLUMN pin_hash TEXT")

conn.commit()
print("managers.pin_hash column added.")
conn.close()
