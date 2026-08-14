"""
TEMPORARY: seed (or unseed) a demo roster for Mike, so the Team page can be
reviewed on a phone with real players in it.

This is scaffolding for the mobile work, not league data. It writes only to the
`rosters` table for one manager, and `--unseed` restores whatever was there
before, byte for byte, from a snapshot taken on the first seed.

    python3 scripts/seed_demo_roster.py --seed
    python3 scripts/seed_demo_roster.py --unseed

Squad shape matches what app.py enforces for a legal lineup: 1 GK, 4 DEF,
4 MID, 2 FW starting, plus a five-man bench and one player on IR. Players are
picked from the top of the 2026-27 projections so the page shows realistic
names, clubs, and numbers rather than placeholder rows.

Delete this file once the mobile review is finished.
"""
import argparse
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'data', 'fantasia.db')
SNAPSHOT = os.path.join(REPO, 'data', '.demo_roster_snapshot.json')

MANAGER_ID = 1          # Mike
GW_START = 1

STARTERS = [
    ('Jordan Pickford',     'GK'),
    ('Lewis Hall',          'DEF'),
    ('James Tarkowski',     'DEF'),
    ('Daniel Muñoz',        'DEF'),
    ('Matty Cash',          'DEF'),
    ('Bruno Fernandes',     'MID'),
    ('Bukayo Saka',         'MID'),
    ('Bruno Guimarães',     'MID'),
    ('Dominik Szoboszlai',  'MID'),
    ('Erling Haaland',      'FW'),
    ('Igor Thiago',         'FW'),
]
BENCH = [
    'Dean Henderson',
    'Anton Stach',
    'Matheus Cunha',
    'Jean-Philippe Mateta',
    'Dominic Solanke',
]
IR = ['Enzo Le Fée']


def check_players_exist(conn):
    wanted = [n for n, _ in STARTERS] + BENCH + IR
    have = {r[0] for r in conn.execute(
        'SELECT name FROM players WHERE name IN (%s)' % ','.join('?' * len(wanted)), wanted)}
    missing = [n for n in wanted if n not in have]
    if missing:
        sys.exit('These players are not in the players table:\n  ' + '\n  '.join(missing))


def seed(conn):
    if os.path.exists(SNAPSHOT):
        sys.exit(f'Snapshot already exists ({SNAPSHOT}) — already seeded. '
                 'Run --unseed first if you want to reseed.')

    check_players_exist(conn)

    existing = [dict(r) for r in conn.execute(
        'SELECT manager_id, player_name, slot_type, position_slot, gw_start, gw_end '
        'FROM rosters WHERE manager_id=?', (MANAGER_ID,))]
    with open(SNAPSHOT, 'w') as fh:
        json.dump(existing, fh, indent=1)
    print(f'Snapshotted {len(existing)} pre-existing roster row(s) -> {SNAPSHOT}')

    conn.execute('DELETE FROM rosters WHERE manager_id=?', (MANAGER_ID,))

    rows = [(MANAGER_ID, n, 'starter', pos, GW_START, None) for n, pos in STARTERS]
    # Bench and IR store the literal slot string in position_slot, matching the
    # convention app.py relies on (real position comes from players.position).
    rows += [(MANAGER_ID, n, 'bench', 'bench', GW_START, None) for n in BENCH]
    rows += [(MANAGER_ID, n, 'ir', 'ir', GW_START, None) for n in IR]

    conn.executemany(
        'INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end) '
        'VALUES (?,?,?,?,?,?)', rows)
    conn.commit()
    print(f'Seeded {len(rows)} rows for manager {MANAGER_ID}: '
          f'{len(STARTERS)} starters, {len(BENCH)} bench, {len(IR)} IR.')


def unseed(conn):
    if not os.path.exists(SNAPSHOT):
        sys.exit('No snapshot found — nothing to restore. '
                 'Refusing to delete rows I did not create.')

    with open(SNAPSHOT) as fh:
        original = json.load(fh)

    conn.execute('DELETE FROM rosters WHERE manager_id=?', (MANAGER_ID,))
    if original:
        conn.executemany(
            'INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end) '
            'VALUES (:manager_id,:player_name,:slot_type,:position_slot,:gw_start,:gw_end)',
            original)
    conn.commit()
    os.remove(SNAPSHOT)
    print(f'Removed demo roster and restored {len(original)} original row(s). '
          'Snapshot deleted.')


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--seed', action='store_true')
    g.add_argument('--unseed', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    (seed if args.seed else unseed)(conn)

    n = conn.execute('SELECT COUNT(*) FROM rosters WHERE manager_id=?', (MANAGER_ID,)).fetchone()[0]
    print(f'Manager {MANAGER_ID} now has {n} roster row(s).')
    conn.close()


if __name__ == '__main__':
    main()
