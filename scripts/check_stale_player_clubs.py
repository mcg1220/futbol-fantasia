"""
Read-only report: players whose `players.club` doesn't match the club
they actually played for in their most recent real match this season.

Why this matters (found while investigating a report that Iliman Ndiaye
shows the wrong club despite his stats being correct): raw_stats.club is
always right -- it's written directly from what the scraper reads off
WhoScored for that specific match, independent of the players table. But
several things in app.py DO trust the (separately-maintained, manually
updated) players.club field to figure out "which match did this player
play this gw":

  - team()'s points_map (app.py ~1575-1600): looks up matches via
    club_matches.get(players.club) rather than joining raw_stats by
    player_name directly -- if players.club is stale, this can attribute
    the wrong match (or no match at all) to a player, corrupting their
    displayed GW Pts / season total / season avg on the Team page.
  - The same route's DEF/GK Clean Sheet / Goals Conceded display
    (app.py ~1637-1657) has the identical bug -- it derives match_id from
    players.club too, so a stale club can show a wrong or missing clean
    sheet for a defender/keeper even though their actual fantasy score
    (computed elsewhere via calc_team_score_for_gw, which matches by
    player_name only) is correct.

So a stale players.club doesn't affect real Standings/results, but it
can make the Team page display outright wrong information -- worse for
DEF/GK since clean sheets/goals conceded depend on club correctness,
exactly as flagged.

This only reports -- it doesn't write anything. For each mismatch found,
club correction is the same one-line fix used for Grealish/Konsa/Robertson
etc. (UPDATE players SET club=? WHERE name=?), it just needs a human to
confirm the new club is actually correct before applying it.

Run: python3 check_stale_player_clubs.py [--season 2026-27]
"""
import argparse
import sqlite3

from init_db import DB_PATH

DEFAULT_SEASON = '2026-27'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--season', default=DEFAULT_SEASON)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Every (player, club) pair this season, with the highest gw_number
    # they appeared for that club and how many games -- lets us pick "the
    # club of their most recent appearance" without assuming raw_stats is
    # ordered any particular way.
    rows = c.execute("""
        SELECT rs.player_name, rs.club, MAX(g.gw_number) AS last_gw, COUNT(*) AS games
        FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season=? AND rs.external=0
        GROUP BY rs.player_name, rs.club
    """, (args.season,)).fetchall()

    by_player = {}
    for r in rows:
        by_player.setdefault(r['player_name'], []).append(r)

    players = {p['name']: p for p in c.execute("SELECT name, club, position FROM players").fetchall()}

    mismatches = []
    for name, appearances in by_player.items():
        player = players.get(name)
        if not player:
            continue  # covered separately by check_unmatched_player_names.py
        # Most recent club = the one with the highest last_gw seen this season.
        most_recent = max(appearances, key=lambda a: a['last_gw'])
        if most_recent['club'] != player['club']:
            mismatches.append({
                'name': name,
                'position': player['position'],
                'current_club_on_file': player['club'],
                'actual_recent_club': most_recent['club'],
                'last_gw_for_actual_club': most_recent['last_gw'],
                'games_for_actual_club': most_recent['games'],
                'appearances': sorted(appearances, key=lambda a: -a['last_gw']),
            })

    if not mismatches:
        print(f"No stale players.club found for {args.season} -- every player's club on file "
              f"matches their most recent real appearance.")
        conn.close()
        return

    high_impact = [m for m in mismatches if m['position'] in ('DEF', 'GK')]
    low_impact = [m for m in mismatches if m['position'] not in ('DEF', 'GK')]

    print(f"{len(mismatches)} player(s) with a stale club on file for {args.season} "
          f"({len(high_impact)} DEF/GK -- higher impact, {len(low_impact)} FW/MID).\n")

    for label, group in [("DEF/GK (affects clean sheets / goals conceded display)", high_impact),
                          ("FW/MID (display-only Pts/season-total impact)", low_impact)]:
        if not group:
            continue
        print(f"--- {label} ---")
        for m in group:
            print(f"  {m['name']} ({m['position']}): on file as {m['current_club_on_file']!r}, "
                  f"actually played for {m['actual_recent_club']!r} through GW{m['last_gw_for_actual_club']} "
                  f"({m['games_for_actual_club']} game(s))")
            for a in m['appearances']:
                if a['club'] != m['actual_recent_club']:
                    print(f"      also has {a['games']} game(s) on file for {a['club']!r} (through GW{a['last_gw']})")
        print()

    print("Fix pattern for each (confirm the club is correct first): "
          "UPDATE players SET club=<actual_recent_club> WHERE name=<name>")
    conn.close()


if __name__ == '__main__':
    main()
