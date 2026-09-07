"""
One-off: fill in match_date/kickoff_time for GW6-9's fixtures, which were
loaded with the right home/away pairings but no date/time (day/time data
stopped after GW5).

Source: a 4-part Fantrax export covering GWs 6-9 (one CSV per gameweek,
each player row showing their club's next match as "OPP<br/>Day H:MMAM/PM"
in a day-of-week column, "@OPP" for an away fixture). Parsed and cross-
checked against every existing GW6-9 fixture row's home_club/away_club
pairing before building this: all 40 fixtures (10 per gameweek) matched
an existing row exactly, with zero unparsed cells, conflicting times, or
mismatched pairings. Kickoff times are already Eastern (US East Coast)
in the source data, matching how every other gameweek's kickoff_time is
stored and displayed on the site -- no timezone conversion applied here.

Matches each fixture by (season, gw_number, home_club, away_club) rather
than match_id, since these fixtures don't have a real WhoScored match_id
yet (they haven't been played).

    python3 update_gw6_9_fixture_times.py            # dry run
    python3 update_gw6_9_fixture_times.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

SEASON = '2026-27'

# (gw_number, home_club, away_club, match_date, kickoff_time)
FIXTURES = [
    (6, 'Manchester United', 'Tottenham', '2026-10-10', '12:30'),
    (6, 'Liverpool', 'Manchester City', '2026-10-11', '11:30'),
    (6, 'Hull', 'Everton', '2026-10-11', '09:00'),
    (6, 'Chelsea', 'Bournemouth', '2026-10-10', '10:00'),
    (6, 'Arsenal', 'Leeds', '2026-10-10', '07:30'),
    (6, 'Crystal Palace', 'Nottingham Forest', '2026-10-11', '09:00'),
    (6, 'Ipswich', 'Fulham', '2026-10-10', '10:00'),
    (6, 'Sunderland', 'Brighton', '2026-10-10', '10:00'),
    (6, 'Coventry', 'Newcastle', '2026-10-12', '15:00'),
    (6, 'Aston Villa', 'Brentford', '2026-10-10', '10:00'),
    (7, 'Brentford', 'Liverpool', '2026-10-17', '10:00'),
    (7, 'Newcastle', 'Aston Villa', '2026-10-17', '12:30'),
    (7, 'Manchester City', 'Ipswich', '2026-10-17', '10:00'),
    (7, 'Everton', 'Chelsea', '2026-10-17', '07:30'),
    (7, 'Leeds', 'Manchester United', '2026-10-18', '09:00'),
    (7, 'Bournemouth', 'Sunderland', '2026-10-18', '09:00'),
    (7, 'Fulham', 'Hull', '2026-10-17', '10:00'),
    (7, 'Brighton', 'Crystal Palace', '2026-10-18', '09:00'),
    (7, 'Nottingham Forest', 'Arsenal', '2026-10-18', '11:30'),
    (7, 'Tottenham', 'Coventry', '2026-10-19', '15:00'),
    (8, 'Manchester United', 'Bournemouth', '2026-10-25', '10:00'),
    (8, 'Liverpool', 'Brighton', '2026-10-25', '10:00'),
    (8, 'Hull', 'Brentford', '2026-10-25', '10:00'),
    (8, 'Chelsea', 'Tottenham', '2026-10-24', '12:30'),
    (8, 'Arsenal', 'Everton', '2026-10-24', '10:00'),
    (8, 'Crystal Palace', 'Newcastle', '2026-10-25', '10:00'),
    (8, 'Ipswich', 'Nottingham Forest', '2026-10-23', '15:00'),
    (8, 'Sunderland', 'Leeds', '2026-10-25', '12:30'),
    (8, 'Coventry', 'Fulham', '2026-10-24', '10:00'),
    (8, 'Aston Villa', 'Manchester City', '2026-10-24', '07:30'),
    (9, 'Liverpool', 'Arsenal', '2026-11-01', '11:30'),
    (9, 'Brentford', 'Nottingham Forest', '2026-10-31', '11:00'),
    (9, 'Newcastle', 'Everton', '2026-11-02', '15:00'),
    (9, 'Hull', 'Ipswich', '2026-10-31', '11:00'),
    (9, 'Manchester City', 'Brighton', '2026-10-31', '11:00'),
    (9, 'Chelsea', 'Manchester United', '2026-10-31', '08:30'),
    (9, 'Bournemouth', 'Leeds', '2026-10-31', '11:00'),
    (9, 'Tottenham', 'Crystal Palace', '2026-10-31', '13:30'),
    (9, 'Coventry', 'Sunderland', '2026-10-31', '11:00'),
    (9, 'Aston Villa', 'Fulham', '2026-10-31', '16:00'),]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    to_apply = []
    not_found = []
    for gw, home, away, date, time in FIXTURES:
        row = conn.execute("""
            SELECT f.id, f.match_id, f.match_date, f.kickoff_time
            FROM fixtures f JOIN gameweeks g ON g.id = f.gw_id
            WHERE g.season=? AND g.gw_number=? AND f.home_club=? AND f.away_club=?
        """, (SEASON, gw, home, away)).fetchone()
        if not row:
            not_found.append((gw, home, away))
            continue
        if row['match_date'] == date and row['kickoff_time'] == time:
            continue
        to_apply.append((row['id'], gw, home, away, row['match_date'], row['kickoff_time'], date, time))

    if not_found:
        print(f"{len(not_found)} fixture(s) from the source had no matching DB row -- check these manually:")
        for gw, home, away in not_found:
            print(f"  GW{gw}: {home} vs {away}")
        print()

    print(f"{len(to_apply)} fixture(s) to update:")
    for fid, gw, home, away, old_date, old_time, new_date, new_time in to_apply:
        print(f"  GW{gw} {home} vs {away}: ({old_date!r}, {old_time!r}) -> ({new_date!r}, {new_time!r})")

    if not to_apply:
        print("\nNothing to change.")
        conn.close()
        return

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        conn.close()
        return

    for fid, gw, home, away, old_date, old_time, new_date, new_time in to_apply:
        conn.execute("UPDATE fixtures SET match_date=?, kickoff_time=? WHERE id=?", (new_date, new_time, fid))
    conn.commit()
    print(f"\nApplied. {len(to_apply)} fixture(s) updated.")
    conn.close()


if __name__ == '__main__':
    main()
