"""
One-time migration: add indexes on the columns hammered hardest by the
scoring engine and roster lookups. Confirmed via `grep -rn "CREATE INDEX"`
across the repo that no indexes existed anywhere before this -- every
raw_stats/rosters lookup (thousands per Gameweek/Add-Drop page load) was a
full table scan. Purely additive, no behavior change, safe to run any time.

Run once: python3 migrate_performance_indexes.py
"""
import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("CREATE INDEX IF NOT EXISTS idx_raw_stats_player_name ON raw_stats(player_name)")
c.execute("CREATE INDEX IF NOT EXISTS idx_raw_stats_match_id ON raw_stats(match_id)")
c.execute("CREATE INDEX IF NOT EXISTS idx_rosters_manager_id ON rosters(manager_id)")
c.execute("CREATE INDEX IF NOT EXISTS idx_rosters_player_name ON rosters(player_name)")

conn.commit()
print("Indexes ready: raw_stats(player_name), raw_stats(match_id), rosters(manager_id), rosters(player_name).")
conn.close()
