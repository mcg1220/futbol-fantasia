"""
One-time migration: add scraper_runs.trigger column so auto-triggered runs
(fired 30 min before a gameweek locks) are distinguishable from manual
"Press the Button" runs in the Scraper Log. Defaults existing rows to
'manual', since every run up to now was.

Run once: python3 migrate_scraper_trigger_column.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

cols = [row[1] for row in c.execute("PRAGMA table_info(scraper_runs)").fetchall()]
if 'trigger' not in cols:
    c.execute("ALTER TABLE scraper_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
    conn.commit()
    print("Added scraper_runs.trigger column (default 'manual').")
else:
    print("scraper_runs.trigger already exists — nothing to do.")

conn.close()
