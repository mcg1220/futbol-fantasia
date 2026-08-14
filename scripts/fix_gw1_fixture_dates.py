"""
Repair GW1 fixture dates for 2026-27.

Reported from mobile: the GW1 fixture list showed wrong dates. Five of the ten
GW1 fixtures were sitting on a 2020-01-01 / 10:00 placeholder rather than their
real kickoff:

    Brentford v Tottenham, Brighton v Aston Villa, Manchester City v
    Bournemouth, Newcastle v Liverpool, Fulham v Chelsea

The other five GW1 fixtures, and all of GW2-5, were already correct. GW6-38
have NULL dates, which is expected -- the Fantrax export only covers GW1-5 and
the UI handles missing dates already. So this is a narrow, one-off repair.

Source of truth is the same Fantrax schedule export the original import used
(Schedule (1 of 5).csv), whose header carries the GW1 date columns
("Fri 8/21" ... "Mon 8/24") and whose cells look like "@HUL<br/>Sat 7:30AM".
Times are US Eastern, matching what is already stored for the correct rows.

The script derives all ten fixtures from the CSV rather than hardcoding the
five, so the five known-good rows act as a built-in check: if our parse of
those disagrees with what is already in the database, something is wrong with
the parse and we abort instead of writing.

Idempotent: rows already holding the right values are left alone.

    python3 scripts/fix_gw1_fixture_dates.py            # dry run
    python3 scripts/fix_gw1_fixture_dates.py --apply
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'data', 'fantasia.db')
CSV_PATH = os.path.join(
    REPO, 'fantrax schedule and projections',
    'Fantrax-Players-Fantasy Futbol - Schedule (1 of 5).csv',
)
SEASON = '2026-27'
GW = 1

# Same mapping the original import uses (scripts/apply_fantrax_import.py:27).
TEAM_MAP = {
    'ARS': 'Arsenal', 'AVL': 'Aston Villa', 'BHA': 'Brighton', 'BOU': 'Bournemouth',
    'BRF': 'Brentford', 'CHE': 'Chelsea', 'COV': 'Coventry', 'CRY': 'Crystal Palace',
    'EVE': 'Everton', 'FUL': 'Fulham', 'HUL': 'Hull', 'IPS': 'Ipswich', 'LEE': 'Leeds',
    'LIV': 'Liverpool', 'MCI': 'Manchester City', 'MUN': 'Manchester United',
    'NEW': 'Newcastle', 'NOT': 'Nottingham Forest', 'SUN': 'Sunderland', 'TOT': 'Tottenham',
}

CELL_RE = re.compile(r'(@?)([A-Z]{2,4})<br/>\w{3}\s+(.+)$')


def parse_schedule():
    """-> {(home_club, away_club): (YYYY-MM-DD, HH:MM)} for all GW1 fixtures."""
    with open(CSV_PATH, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))

    # Date columns are the header cells that look like "Fri 8/21".
    date_cols = [c for c in rows[0].keys() if re.fullmatch(r'\w{3} \d{1,2}/\d{1,2}', c or '')]
    if not date_cols:
        sys.exit('Could not find date columns in the schedule CSV header.')

    # The export omits the year; GW1 of 2026-27 is August 2026.
    year = int(SEASON.split('-')[0])

    fixtures = {}
    for row in rows:
        team_code = (row.get('Team') or '').strip()
        if team_code not in TEAM_MAP:
            continue
        for col in date_cols:
            cell = (row.get(col) or '').strip()
            if not cell:
                continue
            m = CELL_RE.match(cell)
            if not m:
                continue
            away_flag, opp_code, time_str = m.groups()
            if opp_code not in TEAM_MAP:
                continue
            team, opp = TEAM_MAP[team_code], TEAM_MAP[opp_code]
            home, away = (opp, team) if away_flag else (team, opp)

            month, day = col.split(' ')[1].split('/')
            date = f'{year}-{int(month):02d}-{int(day):02d}'
            kickoff = datetime.strptime(time_str.strip().upper(), '%I:%M%p').strftime('%H:%M')
            fixtures[(home, away)] = (date, kickoff)
    return fixtures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    parsed = parse_schedule()
    print(f'Parsed {len(parsed)} GW{GW} fixtures from the Fantrax schedule export.\n')

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT f.id, f.home_club, f.away_club, f.match_date, f.kickoff_time
             FROM fixtures f JOIN gameweeks g ON g.id = f.gw_id
            WHERE g.gw_number = ? AND g.season = ?
            ORDER BY f.id""",
        (GW, SEASON),
    ).fetchall()

    if not rows:
        sys.exit(f'No GW{GW} fixtures found for {SEASON}.')

    updates, unchanged, missing, conflicts = [], [], [], []
    for r in rows:
        key = (r['home_club'], r['away_club'])
        if key not in parsed:
            missing.append(r)
            continue
        date, kickoff = parsed[key]
        if r['match_date'] == date and r['kickoff_time'] == kickoff:
            unchanged.append(r)
        elif r['match_date'] not in (None, '', '2020-01-01'):
            # A real-looking date that disagrees with the CSV: do not silently
            # overwrite. These are the rows that validate the parse.
            conflicts.append((r, date, kickoff))
        else:
            updates.append((r, date, kickoff))

    if unchanged:
        print(f'Already correct ({len(unchanged)}) — these confirm the parse matches reality:')
        for r in unchanged:
            print(f"    {r['home_club']:18} v {r['away_club']:18}  {r['match_date']} {r['kickoff_time']}")
        print()

    if conflicts:
        print(f'CONFLICT ({len(conflicts)}) — stored date looks real but disagrees with the CSV:')
        for r, d, k in conflicts:
            print(f"    {r['home_club']:18} v {r['away_club']:18}  stored {r['match_date']} {r['kickoff_time']}  vs csv {d} {k}")
        print('\nAborting without writing — resolve this by hand first.')
        sys.exit(1)

    if missing:
        print(f'WARNING: {len(missing)} fixture(s) in the DB had no match in the CSV:')
        for r in missing:
            print(f"    {r['home_club']} v {r['away_club']}")
        print()

    if not updates:
        print('Nothing to fix — all GW1 fixtures already hold the right date.')
        return

    print(f'To fix ({len(updates)}):')
    for r, d, k in updates:
        print(f"    {r['home_club']:18} v {r['away_club']:18}  {r['match_date']} {r['kickoff_time']}  ->  {d} {k}")

    if not args.apply:
        print('\nDry run. Re-run with --apply to write.')
        return

    for r, d, k in updates:
        conn.execute('UPDATE fixtures SET match_date=?, kickoff_time=? WHERE id=?', (d, k, r['id']))
    conn.commit()
    print(f'\nApplied {len(updates)} fixture date corrections.')
    conn.close()


if __name__ == '__main__':
    main()
