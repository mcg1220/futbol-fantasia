"""
One-off: give Christos Tzolis (Arsenal) both MID and FW eligibility.

    python3 add_tzolis_mid_eligibility.py            # dry run
    python3 add_tzolis_mid_eligibility.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

NAME = "Christos Tzolis"
POSITIONS = ["MID", "FW"]
SOURCE = "manual_add"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    player = conn.execute("SELECT id FROM players WHERE name=?", (NAME,)).fetchone()
    if not player:
        print(f"No player named {NAME!r} found -- nothing to do.")
        return
    player_id = player['id']

    existing = {r['position'] for r in conn.execute(
        "SELECT position FROM player_eligibility WHERE player_id=?", (player_id,)
    ).fetchall()}
    missing = [p for p in POSITIONS if p not in existing]

    if not missing:
        print(f"{NAME!r} (id={player_id}) already has {sorted(existing)} eligibility -- nothing to do.")
        return

    print(f"{NAME!r} (id={player_id}) currently eligible for {sorted(existing) or '(none)'}.")
    print(f"Will add player_eligibility row(s) for: {missing} (source={SOURCE!r}).")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    for pos in missing:
        conn.execute(
            "INSERT INTO player_eligibility (player_id, position, source) VALUES (?, ?, ?)",
            (player_id, pos, SOURCE)
        )
    conn.commit()
    print(f"\nApplied — {NAME!r} is now eligible for {sorted(existing | set(missing))}.")
    conn.close()


if __name__ == '__main__':
    main()
