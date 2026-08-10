"""
One-time migration: add tables for in-season player trades
(player_trades, player_trade_items).

Run once: python3 migrate_player_trades.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS player_trades (
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
    CREATE TABLE IF NOT EXISTS player_trade_items (
        id INTEGER PRIMARY KEY,
        trade_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        from_manager_id INTEGER NOT NULL,
        to_manager_id INTEGER NOT NULL,
        FOREIGN KEY (trade_id) REFERENCES player_trades(id)
    )
""")

conn.commit()
print("player_trades, player_trade_items tables created.")
conn.close()
