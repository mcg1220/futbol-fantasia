"""
One-time migration: add player_trades.proposer_notified, used to show the
proposer a one-time "your trade was accepted" confirmation banner.

Run once: python3 migrate_trade_notifications.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

cols = [r[1] for r in c.execute("PRAGMA table_info(player_trades)").fetchall()]
if 'proposer_notified' not in cols:
    c.execute("ALTER TABLE player_trades ADD COLUMN proposer_notified INTEGER NOT NULL DEFAULT 0")
    print("Added player_trades.proposer_notified")
else:
    print("player_trades.proposer_notified already exists")

conn.commit()
conn.close()
