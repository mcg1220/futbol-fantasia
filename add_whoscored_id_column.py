"""
One-time migration: add whoscored_id column to players table.
Safe to re-run — checks if the column already exists first.

Run from futbol_fantasia/ folder:
    python add_whoscored_id_column.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

cols = [row[1] for row in c.execute("PRAGMA table_info(players)").fetchall()]
if 'whoscored_id' not in cols:
    c.execute("ALTER TABLE players ADD COLUMN whoscored_id INTEGER")
    conn.commit()
    print("Added 'whoscored_id' column to players table.")
else:
    print("'whoscored_id' column already exists — nothing to do.")

conn.close()
