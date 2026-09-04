"""
One-off insert: exempt Jack's Moisés Caicedo from the GW3 IR eligibility
check.

Caicedo was already on IR during GW2 (an earlier injury), returned to
Chelsea's matchday squad that week, but got hurt again during that very
game -- a genuine, rare re-injury, not IR abuse. The automated check
can't tell that apart from someone parking a healthy player on IR (both
look identical: "on IR at gw-1, appeared in gw-1's squad anyway"), so
per the commissioner this is a manual, one-time exception for this
specific gameweek rather than a change to the rule itself.

Looks the manager up by name rather than a hardcoded id, since manager
ids can differ between environments. Requires
scripts/migrate_ir_eligibility_overrides.py to have already been run.

Run once: python3 add_ir_eligibility_override.py
"""
import sqlite3
from datetime import datetime

from init_db import DB_PATH

SEASON = '2026-27'
MANAGER_NAME = 'Jack'
PLAYER_NAME = 'Moisés Caicedo'
GW = 3
REASON = 'Was already on IR during GW2, returned to the Chelsea squad, but got hurt again during that ' \
         'same match -- a genuine re-injury, not IR abuse. One-time exception for GW3 only.'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

manager = c.execute("SELECT id, name, team_name FROM managers WHERE name=?", (MANAGER_NAME,)).fetchone()
if not manager:
    raise SystemExit(f"No manager found named {MANAGER_NAME!r} -- check the managers table and fix MANAGER_NAME above.")

existing = c.execute("""
    SELECT id FROM ir_eligibility_overrides WHERE season=? AND manager_id=? AND player_name=? AND gw=?
""", (SEASON, manager['id'], PLAYER_NAME, GW)).fetchone()
if existing:
    print(f"Override already exists for {manager['name']} / {PLAYER_NAME} / GW{GW} (id={existing['id']}) -- nothing to do.")
else:
    c.execute("""
        INSERT INTO ir_eligibility_overrides (season, manager_id, player_name, gw, reason, created_at)
        VALUES (?,?,?,?,?,?)
    """, (SEASON, manager['id'], PLAYER_NAME, GW, REASON, datetime.now().isoformat()))
    conn.commit()
    print(f"Added IR eligibility override for {manager['name']} ({manager['team_name']}) -- "
          f"{PLAYER_NAME}, GW{GW}, {SEASON}.")

conn.close()
