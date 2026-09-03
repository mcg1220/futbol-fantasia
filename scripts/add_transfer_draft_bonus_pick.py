"""
One-off insert: Tom & Brian's Summer Transfer Draft compensation pick.

Ollie Watkins (drafted by Tom & Brian in the first 4 rounds of the Main
Draft) transferred out of the Premier League to a Saudi Arabian club before
the August 31st deadline. Per league rule, that entitles Tom & Brian to a
pick before Round 1 of the Summer Transfer Draft -- ahead of Jack, who
otherwise holds the #1 Round 1 pick via the Summer Transfer Token.

This inserts one row into transfer_draft_bonus_picks (see
scripts/migrate_transfer_draft_bonus_picks.py for that table, which must
already exist before running this). transfer_draft_start() reads this table
when the Summer draft is started and seeds it as a round-0 pick ahead of
Round 1 -- so this must run BEFORE clicking "Start Summer Transfer Draft".

Looks the manager up by name rather than a hardcoded id, since manager ids
can differ between environments.

Run once: python3 add_transfer_draft_bonus_pick.py
"""
import sqlite3
from init_db import DB_PATH

SEASON = '2026-27'
DRAFT_TYPE = 'summer'
MANAGER_NAME = 'Tom & Brian'
REASON = 'Ollie Watkins (drafted in Round <=4 of the Main Draft) transferred out of the Premier League to Saudi Arabia before the Aug 31 deadline'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

manager = c.execute("SELECT id, name, team_name FROM managers WHERE name=?", (MANAGER_NAME,)).fetchone()
if not manager:
    raise SystemExit(f"No manager found named {MANAGER_NAME!r} -- check the managers table and fix MANAGER_NAME above.")

existing = c.execute("""
    SELECT id FROM transfer_draft_bonus_picks WHERE season=? AND draft_type=? AND manager_id=?
""", (SEASON, DRAFT_TYPE, manager['id'])).fetchone()
if existing:
    print(f"{manager['name']} already has a {DRAFT_TYPE} {SEASON} bonus pick (id={existing['id']}) -- nothing to do.")
else:
    from datetime import datetime
    c.execute("""
        INSERT INTO transfer_draft_bonus_picks (season, draft_type, manager_id, reason, created_at)
        VALUES (?,?,?,?,?)
    """, (SEASON, DRAFT_TYPE, manager['id'], REASON, datetime.now().isoformat()))
    conn.commit()
    print(f"Added bonus pick for {manager['name']} ({manager['team_name']}) -- {DRAFT_TYPE} {SEASON}.")

conn.close()
