"""
One-time migration: transfer_draft_bonus_picks table.

Supports the rule: a manager who drafted a player in the first 4 rounds of
the Main Draft, and that player transferred out of the Premier League
before the relevant deadline, is entitled to a compensation pick before
Round 1 of the Summer (or Winter) Transfer Draft. There's no reliable
signal in the schema for "this player left the league on this date" (see
players.club, which is just overwritten in place with no history), so
this is deliberately a manually-curated list, not something auto-detected
-- insert a row here by hand whenever this comes up (see
scripts/add_transfer_draft_bonus_pick.py for the one-off insert pattern),
before starting the affected transfer draft.

transfer_draft_start() reads this table when building a draft's pick
order and, if any rows exist for that season/draft_type, seeds them as
round=0 entries ahead of the normal round 1/2 order.

Run once: python3 migrate_transfer_draft_bonus_picks.py
"""
import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_draft_bonus_picks (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        manager_id INTEGER NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("transfer_draft_bonus_picks table ready.")
conn.close()
