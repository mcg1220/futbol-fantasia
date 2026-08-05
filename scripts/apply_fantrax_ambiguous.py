"""
Step 2b of the Fantrax import: apply the user's resolutions for the 36
ambiguous players (reviewed via the interactive Artifact).

- Merge the one remaining genuine DB duplicate (Ali Al-Hamadi / Ali Al Hamadi)
- Update club for matches where the DB player's club differs from Fantrax's
  (either a real transfer, or filling in a previously-blank club)
- Insert the confirmed new players, excluding those with proj_total == 0
  (same rule the user gave for the original new-player batch)
- Upsert player_projections for every one of the 36, keyed to the correct
  (possibly pre-existing) DB player name

Run: python3 apply_fantrax_ambiguous.py
"""

import sqlite3
from init_db import DB_PATH

SEASON = '2026-27'

# (fantrax_club, fpts, fpg, db_name)
MATCHES = [
    ('Tottenham', 250.42, 8.35, 'Mateus Fernandes'),
    ('Sunderland', 234.06, 7.55, 'Dan Ballard'),
    ('Newcastle', 215.72, 6.34, 'Tino Livramento'),
    ('Liverpool', 207.49, 6.29, 'Alisson Becker'),
    ('Sunderland', 203, 6.77, 'Reinildo Mandava'),
    ('Liverpool', 41.15, 13.72, 'Joe Gomez'),
    ('Coventry', 31.88, 6.38, 'Kaine Kesler-Hayden'),
    ('Manchester City', 24.33, 8.11, 'Josh Wilson-Esbrand'),
    ('Tottenham', 20.3, 10.15, 'Yang Min-Hyeok'),
    ('Liverpool', 19.8, 9.9, 'Kostas Tsimikas'),
    ('Nottingham Forest', 17.96, 5.99, 'John Victor'),
    ('Aston Villa', 17.69, 8.84, 'Alysson Edward'),
    ('Chelsea', 12.48, 6.24, 'Filip Jörgensen'),
    ('Ipswich', 10.1, 5.05, 'Ali Al Hamadi'),
    ('Sunderland', 9.3, 9.3, 'Djiamgone Jocelin Ta Bi'),
    ('Sunderland', 8.69, 8.69, 'Leo Hjelde'),
    ('Coventry', 1.44, 0, 'Kai Andrews'),
    ('Brentford', 0.71, 0, 'Kim Ji-Soo'),
    ('Bournemouth', 0, 0, 'Will Dennis'),
    ('Sunderland', 0, 0, 'Jaydon Jones'),
    ('Newcastle', 0, 0, 'Park Seung-Soo'),
    ('Liverpool', 0, 0, 'Calum Scanlon'),
    ('Chelsea', 0, 0, 'Gaga Slonina'),
    ('Crystal Palace', 0, 0, 'Joe Whitworth'),
    ('Tottenham', 0, 0, 'Lucá Williams-Barnett'),
]

# (fantrax_name, club, position, fpts, fpg) — excludes fpts == 0 candidates
# (Aidan Harris, Mason Miley, Charlie Walker-Smith, Will Wright)
NEW_PLAYERS = [
    ('Jay da Silva', 'Coventry', 'DEF', 206.69, 6.26),
    ('Emersonn Correia da Silva', 'Ipswich', 'FW', 192.48, 6.64),
    ('Bazoumana Toure', 'Newcastle', 'FW', 79.68, 8.85),
    ('Alvaro Rodriguez', 'Bournemouth', 'FW', 56.83, 8.12),
    ('Jaden Heskey', 'Manchester City', 'FW', 38.12, 5.45),
    ('Amario Cozier-Duberry', 'Brighton', 'MID', 13.17, 6.59),
    ('Luis Hemir Silva Semedo', 'Sunderland', 'FW', 8.34, 8.34),
]

DUPLICATE_MERGE = (674, 1291)  # Ali Al-Hamadi (no club) -> Ali Al Hamadi (Ipswich)


def main():
    conn = sqlite3.connect(DB_PATH)

    print("1. Merging Ali Al-Hamadi duplicate...")
    dupe_id, keep_id = DUPLICATE_MERGE
    dupe = conn.execute("SELECT name FROM players WHERE id=?", (dupe_id,)).fetchone()
    keep = conn.execute("SELECT name FROM players WHERE id=?", (keep_id,)).fetchone()
    if dupe and keep:
        conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (dupe_id,))
        conn.execute("DELETE FROM players WHERE id=?", (dupe_id,))
        print(f"  Merged '{dupe[0]}' into '{keep[0]}'")
    else:
        print("  SKIP: one of the rows is already gone")

    print("2. Updating club + projections for 25 confirmed matches...")
    club_updates = 0
    for fantrax_club, fpts, fpg, db_name in MATCHES:
        row = conn.execute("SELECT club FROM players WHERE name=?", (db_name,)).fetchone()
        if not row:
            print(f"  WARN: no DB row found for '{db_name}'")
            continue
        if row[0] != fantrax_club:
            conn.execute("UPDATE players SET club=? WHERE name=?", (fantrax_club, db_name))
            club_updates += 1
        conn.execute("""
            INSERT INTO player_projections (season, player_name, proj_total, proj_avg)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(season, player_name) DO UPDATE SET
                proj_total = excluded.proj_total, proj_avg = excluded.proj_avg
        """, (SEASON, db_name, float(fpts), float(fpg)))
    print(f"  {club_updates} club updates, 25 projection rows upserted")

    print("3. Inserting confirmed new players...")
    for name, club, pos, fpts, fpg in NEW_PLAYERS:
        cur = conn.execute(
            "INSERT INTO players (name, club, position) VALUES (?, ?, ?)",
            (name, club, pos)
        )
        conn.execute(
            "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
            (cur.lastrowid, pos, 'fantrax_import_placeholder')
        )
        conn.execute("""
            INSERT INTO player_projections (season, player_name, proj_total, proj_avg)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(season, player_name) DO UPDATE SET
                proj_total = excluded.proj_total, proj_avg = excluded.proj_avg
        """, (SEASON, name, float(fpts), float(fpg)))
    print(f"  Inserted {len(NEW_PLAYERS)} new players")

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == '__main__':
    main()
