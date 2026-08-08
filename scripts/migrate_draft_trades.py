"""
One-time migration: add tables for the Main Draft pick-trading feature
(draft_pick_trades, draft_pick_trade_items, draft_pick_ownership).

Run once: python3 migrate_draft_trades.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_pick_trades (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        proposer_manager_id INTEGER NOT NULL,
        target_manager_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        responded_at TEXT,
        FOREIGN KEY (proposer_manager_id) REFERENCES managers(id),
        FOREIGN KEY (target_manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_pick_trade_items (
        id INTEGER PRIMARY KEY,
        trade_id INTEGER NOT NULL,
        overall_pick INTEGER NOT NULL,
        from_manager_id INTEGER NOT NULL,
        to_manager_id INTEGER NOT NULL,
        FOREIGN KEY (trade_id) REFERENCES draft_pick_trades(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS draft_pick_ownership (
        season TEXT NOT NULL,
        overall_pick INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        PRIMARY KEY (season, overall_pick)
    )
""")

conn.commit()
print("draft_pick_trades, draft_pick_trade_items, draft_pick_ownership tables created.")
conn.close()
