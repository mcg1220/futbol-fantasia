"""
One-off follow-up to add_pep_chavarria.py: he's also MID-eligible per
WhoScored (whoscored.com/players/448468/show/pep-chavarria lists
"Defender (Left), Midfielder (Left)"), not DEF-only as first added.

    python3 add_pep_chavarria_mid_eligibility.py            # dry run
    python3 add_pep_chavarria_mid_eligibility.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

NAME = "Pep Chavarría"
POSITION = "MID"
SOURCE = "manual_add"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    player = conn.execute("SELECT id FROM players WHERE name=?", (NAME,)).fetchone()
    if not player:
        print(f"No player named {NAME!r} found -- run add_pep_chavarria.py first.")
        return
    player_id = player['id']

    existing = conn.execute(
        "SELECT 1 FROM player_eligibility WHERE player_id=? AND position=?", (player_id, POSITION)
    ).fetchone()
    if existing:
        print(f"{NAME!r} (id={player_id}) already has {POSITION!r} eligibility — nothing to do.")
        return

    print(f"Will add player_eligibility row: player_id={player_id} ({NAME!r}), position={POSITION!r}, source={SOURCE!r}.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    conn.execute(
        "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
        (player_id, POSITION, SOURCE)
    )
    conn.commit()
    print(f"\nApplied — {NAME!r} is now also eligible at {POSITION!r}.")
    conn.close()


if __name__ == '__main__':
    main()
