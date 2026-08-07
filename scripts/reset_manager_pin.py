"""
One-off: clear a manager's PIN so they drop back into the "set your PIN"
flow next time they visit /login. Use when someone forgets their PIN or
a teammate accidentally claims the wrong name first.

Run: python3 reset_manager_pin.py <manager_id>
"""

import sys
import sqlite3
from init_db import DB_PATH

if len(sys.argv) != 2:
    print("Usage: python3 reset_manager_pin.py <manager_id>")
    sys.exit(1)

manager_id = int(sys.argv[1])

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
row = c.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()
if not row:
    print(f"No manager with id {manager_id}.")
    sys.exit(1)

c.execute("UPDATE managers SET pin_hash=NULL WHERE id=?", (manager_id,))
conn.commit()
print(f"Cleared PIN for {row[0]} (id {manager_id}) — they'll set a new one next login.")
conn.close()
