"""
One-time migration: ir_eligibility_overrides table.

Supports a manual, one-off exception to the IR eligibility check for a
specific manager/player/gameweek -- for the rare case where a player was
genuinely re-injured during the very match that would otherwise make them
look "not actually unavailable" (see check_ir_eligibility's docstring).
There's no way to distinguish that from real IR abuse purely from the
data, so this is deliberately a manually-curated, per-gameweek exception,
not a general bypass -- insert a row here by hand whenever this comes up
(see scripts/add_ir_eligibility_override.py for the one-off insert
pattern), before that gameweek closes.

check_ir_eligibility() checks this table first and returns ok=True
immediately if a matching (season, manager_id, player_name, gw) row
exists, skipping the normal check entirely for that one gameweek.

Run once: python3 migrate_ir_eligibility_overrides.py
"""
import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS ir_eligibility_overrides (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        manager_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        gw INTEGER NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("ir_eligibility_overrides table ready.")
conn.close()
