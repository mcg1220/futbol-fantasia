"""
One-time migration: replace transfer_journalists.active (bool) with a
status column ('pending' | 'active' | 'rejected') to support the
propose/approve/reject workflow, plus proposed_by/review_note/reviewed_at.

Run once: python3 migrate_transfer_journalists_status.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

existing_cols = {row[1] for row in c.execute("PRAGMA table_info(transfer_journalists)").fetchall()}

if 'status' not in existing_cols:
    c.execute("ALTER TABLE transfer_journalists ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
if 'proposed_by' not in existing_cols:
    c.execute("ALTER TABLE transfer_journalists ADD COLUMN proposed_by TEXT")
if 'review_note' not in existing_cols:
    c.execute("ALTER TABLE transfer_journalists ADD COLUMN review_note TEXT")
if 'reviewed_at' not in existing_cols:
    c.execute("ALTER TABLE transfer_journalists ADD COLUMN reviewed_at TEXT")

# Backfill from the old `active` boolean, if present.
if 'active' in existing_cols:
    c.execute("UPDATE transfer_journalists SET status='active' WHERE active=1 AND status='pending'")
    c.execute("UPDATE transfer_journalists SET status='rejected' WHERE active=0 AND status='pending'")

conn.commit()
print("transfer_journalists migrated to status-based workflow.")
conn.close()
