"""
One-time migration: add transactions.to_ir column so the Transaction Log and
Audit History can distinguish "added straight to IR" from a normal open-
roster-spot add. Defaults existing rows to 0 -- the straight-to-IR add path
didn't exist before this, so nothing retroactively qualifies.

Run once: python3 migrate_transactions_to_ir_column.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

cols = [row[1] for row in c.execute("PRAGMA table_info(transactions)").fetchall()]
if 'to_ir' not in cols:
    c.execute("ALTER TABLE transactions ADD COLUMN to_ir INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    print("Added transactions.to_ir column (default 0).")
else:
    print("transactions.to_ir already exists — nothing to do.")

conn.close()
