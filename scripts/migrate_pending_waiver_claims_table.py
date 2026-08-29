"""
One-time migration for the locked-pickup-to-waiver feature:

1. New pending_waiver_claims table -- staging area for a free-agent pickup
   attempted while its target is already locked (their club has kicked
   off) but NO waiver window is currently open. We can't just auto-open a
   window for this (it's a single global toggle that would collaterally
   convert every OTHER manager's still-valid instant "Add" for a
   not-yet-played player into a slow "Claim" too) -- so these rows just
   wait here and get promoted into a real waiver_claims row the next time
   a window is manually opened (see waiver_open()).

2. waiver_claims.to_ir column -- lets a waiver claim (real or pending)
   land its incoming player directly on IR, same as the instant-add path
   already supports. Previously no waiver claim could ever land on IR.

Run once: python3 migrate_pending_waiver_claims_table.py
"""
import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS pending_waiver_claims (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        manager_id INTEGER NOT NULL,
        add_player TEXT NOT NULL,
        drop_player TEXT,
        to_ir INTEGER NOT NULL DEFAULT 0,
        gw INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")
print("pending_waiver_claims table ready.")

cols = [row[1] for row in c.execute("PRAGMA table_info(waiver_claims)").fetchall()]
if 'to_ir' not in cols:
    c.execute("ALTER TABLE waiver_claims ADD COLUMN to_ir INTEGER NOT NULL DEFAULT 0")
    print("Added waiver_claims.to_ir column (default 0).")
else:
    print("waiver_claims.to_ir already exists — nothing to do.")

conn.commit()
conn.close()
