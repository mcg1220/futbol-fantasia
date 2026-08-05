"""
Fútbol de Fantasía - Roster Seeder
Seeds rosters for Mike and Remy across all 5 validated GWs.
"""

import sqlite3
from init_db import DB_PATH


def seed_rosters(conn):
    c = conn.cursor()
    c.execute("DELETE FROM rosters")

    mike_id = c.execute("SELECT id FROM managers WHERE name='Mike'").fetchone()[0]
    remy_id = c.execute("SELECT id FROM managers WHERE name='Remy'").fetchone()[0]

    rosters = {
        # ── GW5 ────────────────────────────────────────────────────────────
        5: {
            mike_id: [
                ("starter", "GK",    "Caoimhín Kelleher"),
                ("starter", "DEF",   "Rodri"),
                ("starter", "DEF",   "Fabian Schär"),
                ("starter", "DEF",   "Gabriel Magalhães"),
                ("starter", "DEF",   "Emmanuel Agbadou"),
                ("starter", "MID",   "Adrien Truffert"),
                ("starter", "MID",   "João Pedro"),
                ("starter", "MID",   "Dominik Szoboszlai"),
                ("starter", "MID",   "Florentino"),
                ("starter", "FW",    "Erling Haaland"),
                ("starter", "FW",    "Jack Grealish"),
                ("bench",   "bench", "Vitalii Mykolenko"),
                ("bench",   "bench", "Antonee Robinson"),
                ("bench",   "bench", "Justin Kluivert"),
                ("bench",   "bench", "Bruno Guimarães"),
                ("ir",      "ir",    "Matheus Cunha"),
            ],
            remy_id: [
                ("starter", "GK",    "Guglielmo Vicario"),
                ("starter", "DEF",   "Nico O'Reilly"),
                ("starter", "DEF",   "Anton Stach"),
                ("starter", "DEF",   "Marcos Senesi"),
                ("starter", "DEF",   "Lewis Hall"),
                ("starter", "MID",   "Declan Rice"),
                ("starter", "MID",   "Jérémy Doku"),
                ("starter", "MID",   "Yéremy Pino"),
                ("starter", "MID",   "Kiernan Dewsbury-Hall"),
                ("starter", "FW",    "Bruno Fernandes"),
                ("starter", "FW",    "Phil Foden"),
                ("bench",   "bench", "Jhon Arias"),
                ("bench",   "bench", "Kevin"),
                ("bench",   "bench", "Dango Ouattara"),
                ("bench",   "bench", "Eberechi Eze"),
            ],
        },
        # ── GW12 ───────────────────────────────────────────────────────────
        12: {
            mike_id: [
                ("starter", "GK",    "Caoimhín Kelleher"),
                ("starter", "DEF",   "Adrien Truffert"),
                ("starter", "DEF",   "Casemiro"),
                ("starter", "DEF",   "Lucas Digne"),
                ("starter", "DEF",   "Fabian Schär"),
                ("starter", "MID",   "Jack Grealish"),
                ("starter", "MID",   "Matheus Cunha"),
                ("starter", "MID",   "João Pedro"),
                ("starter", "MID",   "Bruno Guimarães"),
                ("starter", "FW",    "Erling Haaland"),
                ("starter", "FW",    "Dominik Szoboszlai"),
                ("bench",   "bench", "Rodri"),
                ("bench",   "bench", "Florentino"),
                ("bench",   "bench", "Gabriel Magalhães"),
                ("bench",   "bench", "Justin Kluivert"),
                ("ir",      "ir",    "Antonee Robinson"),
            ],
            remy_id: [
                ("starter", "GK",    "Guglielmo Vicario"),
                ("starter", "DEF",   "Declan Rice"),
                ("starter", "DEF",   "Nordi Mukiele"),
                ("starter", "DEF",   "Marcos Senesi"),
                ("starter", "DEF",   "Dan Ballard"),
                ("starter", "MID",   "Eberechi Eze"),
                ("starter", "MID",   "Jérémy Doku"),
                ("starter", "MID",   "Sean Longstaff"),
                ("starter", "MID",   "Kevin"),
                ("starter", "FW",    "Bruno Fernandes"),
                ("starter", "FW",    "Phil Foden"),
                ("bench",   "bench", "Xavi Simons"),
                ("bench",   "bench", "Kiernan Dewsbury-Hall"),
                ("bench",   "bench", "Rayan Aït-Nouri"),
                ("bench",   "bench", "Rayan Cherki"),
            ],
        },
        # ── GW19 ───────────────────────────────────────────────────────────
        19: {
            mike_id: [
                ("starter", "GK",    "Caoimhín Kelleher"),
                ("starter", "DEF",   "Casemiro"),
                ("starter", "DEF",   "Gabriel Magalhães"),
                ("starter", "DEF",   "Antonee Robinson"),
                ("starter", "DEF",   "Michael Kayode"),
                ("starter", "MID",   "Dominik Szoboszlai"),
                ("starter", "MID",   "Lucas Digne"),
                ("starter", "MID",   "Matheus Cunha"),
                ("starter", "MID",   "Bruno Guimarães"),
                ("starter", "FW",    "Erling Haaland"),
                ("starter", "FW",    "Mikel Merino"),
                ("bench",   "bench", "João Pedro"),
                ("bench",   "bench", "Jack Grealish"),
                ("bench",   "bench", "Ian Maatsen"),
                ("bench",   "bench", "Adrien Truffert"),
                ("ir",      "ir",    "Rodri"),
            ],
            remy_id: [
                ("starter", "GK",    "Guglielmo Vicario"),
                ("starter", "DEF",   "Patrick Dorgu"),
                ("starter", "DEF",   "Lewis Hall"),
                ("starter", "DEF",   "Marcos Senesi"),
                ("starter", "DEF",   "Piero Hincapié"),
                ("starter", "MID",   "Savinho"),
                ("starter", "MID",   "Anton Stach"),
                ("starter", "MID",   "Florian Wirtz"),
                ("starter", "MID",   "Nordi Mukiele"),
                ("starter", "FW",    "Phil Foden"),
                ("starter", "FW",    "Rayan Cherki"),
                ("bench",   "bench", "Dan Ballard"),
                ("bench",   "bench", "Eberechi Eze"),
                ("bench",   "bench", "Bruno Fernandes"),
                ("bench",   "bench", "Declan Rice"),
            ],
        },
        # ── GW26 ───────────────────────────────────────────────────────────
        26: {
            mike_id: [
                ("starter", "GK",    "Caoimhín Kelleher"),
                ("starter", "DEF",   "Rodri"),
                ("starter", "DEF",   "Gabriel Magalhães"),
                ("starter", "DEF",   "Adrien Truffert"),
                ("starter", "DEF",   "Casemiro"),
                ("starter", "MID",   "Matheus Cunha"),
                ("starter", "MID",   "Bruno Guimarães"),
                ("starter", "MID",   "Kobbie Mainoo"),
                ("starter", "MID",   "Amadou Onana"),
                ("starter", "FW",    "Erling Haaland"),
                ("starter", "FW",    "João Pedro"),
                ("bench",   "bench", "Lucas Digne"),
                ("bench",   "bench", "Dominik Szoboszlai"),
                ("bench",   "bench", "Ian Maatsen"),
                ("bench",   "bench", "Antonee Robinson"),
            ],
            remy_id: [
                ("starter", "GK",    "Guglielmo Vicario"),
                ("starter", "DEF",   "Marcos Senesi"),
                ("starter", "DEF",   "Declan Rice"),
                ("starter", "DEF",   "Jurriën Timber"),
                ("starter", "DEF",   "Rayan Aït-Nouri"),
                ("starter", "MID",   "Phil Foden"),
                ("starter", "MID",   "Florian Wirtz"),
                ("starter", "MID",   "Nordi Mukiele"),
                ("starter", "MID",   "Rayan"),
                ("starter", "FW",    "Rayan Cherki"),
                ("starter", "FW",    "Bruno Fernandes"),
                ("bench",   "bench", "Eberechi Eze"),
                ("bench",   "bench", "Anton Stach"),
                ("bench",   "bench", "Jérémy Doku"),
                ("bench",   "bench", "Lewis Hall"),
            ],
        },
        # ── GW33 ───────────────────────────────────────────────────────────
        33: {
            mike_id: [
                ("starter", "GK",    "Caoimhín Kelleher"),
                ("starter", "DEF",   "Rodri"),
                ("starter", "DEF",   "Gabriel Magalhães"),
                ("starter", "DEF",   "Casemiro"),
                ("starter", "DEF",   "Ian Maatsen"),
                ("starter", "MID",   "Alex Scott"),
                ("starter", "MID",   "Kobbie Mainoo"),
                ("starter", "MID",   "Dominik Szoboszlai"),
                ("starter", "MID",   "Adrien Truffert"),
                ("starter", "FW",    "Erling Haaland"),
                ("starter", "FW",    "Matheus Cunha"),
                ("bench",   "bench", "Antonee Robinson"),
                ("bench",   "bench", "Lucas Digne"),
                ("bench",   "bench", "Omar Marmoush"),
                ("bench",   "bench", "João Pedro"),
                ("ir",      "ir",    "Bruno Guimarães"),
            ],
            remy_id: [
                ("starter", "GK",    "Robin Roefs"),
                ("starter", "DEF",   "Declan Rice"),
                ("starter", "DEF",   "Marcos Senesi"),
                ("starter", "DEF",   "Nordi Mukiele"),
                ("starter", "DEF",   "Anton Stach"),
                ("starter", "MID",   "Florian Wirtz"),
                ("starter", "MID",   "Lewis Hall"),
                ("starter", "MID",   "Ethan Ampadu"),
                ("starter", "MID",   "Diego Gómez"),
                ("starter", "FW",    "Rayan Cherki"),
                ("starter", "FW",    "Bruno Fernandes"),
                ("bench",   "bench", "Eberechi Eze"),
                ("bench",   "bench", "Jérémy Doku"),
                ("bench",   "bench", "Phil Foden"),
                ("bench",   "bench", "Rayan"),
            ],
        },
    }

    total = 0
    for gw, manager_rosters in rosters.items():
        for manager_id, players in manager_rosters.items():
            for slot_type, position_slot, player_name in players:
                c.execute("""
                    INSERT INTO rosters
                        (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (manager_id, player_name, slot_type, position_slot, gw, gw))
                total += 1

    conn.commit()
    print(f"Seeded {total} roster entries across {len(rosters)} GWs.")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    seed_rosters(conn)
    conn.close()
    print("Rosters seeded.")
