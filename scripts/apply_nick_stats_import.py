"""
Step 2 of the Nick's-stats import: backfill 2025-26 pre-transfer history for
players who've since moved into the Prem, using the 27 confirmed candidates
from analyze_nick_stats_import.py (João Rego skipped -- no DB match).

Each backfilled row is marked raw_stats.external=1 so the scoring engine
skips goals-conceded/clean-sheet for it (the CSV only has the transferred
player's own team's side of these matches, not the opponent's).

Does NOT touch the shared-match sanity-check corrections -- those were
already applied directly per-row this session.

Run: python3 apply_nick_stats_import.py
"""

import csv
import os
import re
import sqlite3
from init_db import DB_PATH

CSV_PATH = os.environ.get(
    'NICK_CSV_PATH',
    "/Users/michaelgarcia/futbol_fantasia/nick-supplied-data/league_draft_stats_aug2026.csv"
)
DOC_PATH = os.environ.get(
    'NICK_DOC_PATH',
    "/Users/michaelgarcia/futbol_fantasia/nick-supplied-data/league_draft_stats_aug2026_doc"
)

CURRENT_PL_CLUBS = {
    'Arsenal', 'Aston Villa', 'Bournemouth', 'Brentford', 'Brighton', 'Chelsea',
    'Coventry', 'Crystal Palace', 'Everton', 'Fulham', 'Hull', 'Ipswich',
    'Leeds', 'Liverpool', 'Manchester City', 'Manchester United', 'Newcastle',
    'Nottingham Forest', 'Sunderland', 'Tottenham',
}

SKIP_NAMES = {'João Rego'}  # no matching DB player -- skipped per instruction

STAT_COLS = {
    'G': 'goals', 'A': 'assists', 'PKSave': 'pk_saves', 'Ylw': 'yellow_cards',
    'Red': 'red_cards', 'GLC': 'glc', 'LMT': 'lmt', 'ELG': 'elg',
    'OG': 'own_goals', 'MotM': 'motm', 'SoT': 'shots_on_target',
    'KP': 'key_passes', 'Drb': 'dribbles', 'tkl': 'tackles', 'int': 'interceptions',
    'clr': 'clearances', 'blk': 'blocked_shots', 'saves': 'saves',
    'AccCross': 'acc_crosses', 'AccLB': 'acc_long_balls',
}


def to_num(v):
    if v in (None, ''):
        return 0
    return int(float(v))


def derive_minutes(row):
    sub_off = to_num(row.get('SubOffMin'))
    sub_on = to_num(row.get('SubOnMin'))
    if sub_on > 0 and sub_off > 0:
        return sub_off - sub_on
    if sub_off > 0:
        return sub_off
    if sub_on > 0:
        return 90 - sub_on
    return 90


def parse_doc_lists(path):
    """Only the 'external leagues' list needs backfilling. The 'returnees
    from relegated clubs' list (Wolves/West Ham/Burnley) do NOT: those clubs
    played real 2025-26 Premier League fixtures against current-tier
    opponents, so our own scraper already captured them in raw_stats with
    full two-sided match data. Backfilling them too just creates exact
    duplicate rows (caught and cleaned up once already -- see git history)."""
    text = open(path).read()

    def extract_names(block):
        return re.findall(r"'([^']+)'", block)

    included = []
    m = re.search(r"## Includes following players from external leagues:\s*(.*?)\n##", text, re.S)
    if m:
        included += extract_names(m.group(1))
    return included


def main():
    conn = sqlite3.connect(DB_PATH)
    with open(CSV_PATH) as f:
        csv_rows = list(csv.DictReader(f))

    included_names = parse_doc_lists(DOC_PATH)

    inserted_players = 0
    inserted_rows = 0
    skipped_existing = 0

    for name in included_names:
        if name in SKIP_NAMES:
            print(f"Skipping {name} (no DB match)")
            continue

        accent_pairs = [('í','i'),('á','a'),('é','e'),('ó','o'),('ú','u'),('ü','u'),('ñ','n'),
                        ('Í','I'),('Á','A'),('É','E'),('Ó','O'),('Ú','U'),('Ü','U'),('Ñ','N')]

        def strip_accents(s):
            for a, b in accent_pairs:
                s = s.replace(a, b)
            return s

        like_name = strip_accents(name)
        all_players = conn.execute("SELECT name FROM players").fetchall()
        candidates = [p for p in all_players if strip_accents(p[0]) == like_name]
        if len(candidates) != 1:
            print(f"WARNING: {name} did not resolve to exactly one player ({len(candidates)} matches) -- skipping")
            continue
        db_name = candidates[0][0]

        raw_rows = [
            r for r in csv_rows
            if r['player'] == name and r['team'] not in CURRENT_PL_CLUBS
        ]
        seen_match_ids = set()
        rows_for_player = []
        for r in raw_rows:
            if r['matchid'] in seen_match_ids:
                continue
            seen_match_ids.add(r['matchid'])
            rows_for_player.append(r)

        if not rows_for_player:
            continue

        # Any existing row at all (not just external=1) means this match is
        # already covered -- either a prior backfill run, or (as happened
        # once already) a genuinely-scraped PL match for this player that
        # this script has no business touching.
        existing_match_ids = {
            row[0] for row in conn.execute(
                "SELECT match_id FROM raw_stats WHERE player_name = ?",
                (db_name,)
            ).fetchall()
        }

        player_inserted = 0
        for r in rows_for_player:
            match_id = int(r['matchid'])
            if match_id in existing_match_ids:
                skipped_existing += 1
                continue

            values = {db_col: to_num(r[csv_col]) for csv_col, db_col in STAT_COLS.items()}
            minutes = derive_minutes(r)

            cols = ['match_id', 'player_name', 'club', 'gw_number', 'minutes_played', 'external'] + list(values.keys())
            vals = [match_id, db_name, r['team'], 0, minutes, 1] + list(values.values())
            placeholders = ','.join('?' * len(cols))
            conn.execute(
                f"INSERT INTO raw_stats ({', '.join(cols)}) VALUES ({placeholders})",
                vals
            )
            player_inserted += 1
            inserted_rows += 1

        if player_inserted:
            inserted_players += 1
            print(f"{db_name}: inserted {player_inserted} rows")

    conn.commit()
    conn.close()
    print(f"\nDone. {inserted_players} players, {inserted_rows} rows inserted, {skipped_existing} already present (skipped).")


if __name__ == '__main__':
    main()
