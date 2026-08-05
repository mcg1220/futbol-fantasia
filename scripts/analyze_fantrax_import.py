"""
Read-only analysis: cross-reference the Fantrax projected-stats export
against our players table to classify every row as same-club, a transfer,
a genuinely new player, or ambiguous (needs a human call).

Does NOT write to the database. Outputs JSON for the review Artifact.

Run: python3 analyze_fantrax_import.py > /tmp/fantrax_diff.json
"""

import csv
import json
import sqlite3
import unicodedata
from collections import defaultdict
from init_db import DB_PATH

CSV_DIR = "/Users/michaelgarcia/futbol_fantasia/fantrax schedule and projections"
PROJECTIONS_CSV = f"{CSV_DIR}/Fantrax-Players-Fantasy Futbol - Projected Stats.csv"

TEAM_MAP = {
    'ARS': 'Arsenal', 'AVL': 'Aston Villa', 'BHA': 'Brighton', 'BOU': 'Bournemouth',
    'BRF': 'Brentford', 'CHE': 'Chelsea', 'COV': 'Coventry', 'CRY': 'Crystal Palace',
    'EVE': 'Everton', 'FUL': 'Fulham', 'HUL': 'Hull', 'IPS': 'Ipswich', 'LEE': 'Leeds',
    'LIV': 'Liverpool', 'MCI': 'Manchester City', 'MUN': 'Manchester United',
    'NEW': 'Newcastle', 'NOT': 'Nottingham Forest', 'SUN': 'Sunderland', 'TOT': 'Tottenham',
}

POSITION_MAP = {'G': 'GK', 'D': 'DEF', 'M': 'MID', 'F': 'FW'}

# Letters that don't decompose via NFKD (not base+combining-mark in Unicode).
MANUAL_CHAR_MAP = str.maketrans({
    'Ø': 'O', 'ø': 'o', 'Đ': 'D', 'đ': 'd', 'Þ': 'Th', 'þ': 'th',
    'Æ': 'Ae', 'æ': 'ae', 'Œ': 'Oe', 'œ': 'oe', 'ß': 'ss', 'Ł': 'L', 'ł': 'l',
})


def normalize(name):
    name = name.translate(MANUAL_CHAR_MAP)
    name = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    name = name.replace('-', ' ').replace("'", '')
    return ' '.join(name.lower().split())


def load_db_players(conn):
    rows = conn.execute("SELECT name, club, position FROM players").fetchall()
    by_norm = defaultdict(list)
    for name, club, position in rows:
        by_norm[normalize(name)].append({'name': name, 'club': club, 'position': position})
    return by_norm


def fuzzy_candidates(fantrax_name, fclub, by_norm):
    """Token-subset and last-name+club fallback matching for names that
    didn't match exactly even after normalization — e.g. Fantrax listing
    just 'Alisson' for our 'Alisson Becker'."""
    norm_f = normalize(fantrax_name)
    f_tokens = set(norm_f.split())
    candidates = []
    seen = set()
    for norm_db, entries in by_norm.items():
        db_tokens = set(norm_db.split())
        if not f_tokens or not db_tokens:
            continue
        # One name's tokens are a subset of the other's (e.g. "alisson" in "alisson becker").
        if f_tokens <= db_tokens or db_tokens <= f_tokens:
            for e in entries:
                if e['name'] not in seen:
                    candidates.append({**e, 'reason': 'partial name match'})
                    seen.add(e['name'])
            continue
        # Same last name + same destination club (high precision), OR same
        # last name + blank db club (the player exists but we never set a
        # current club for them — e.g. last season's roster — so there's no
        # real "mismatch" to speak of). Deliberately NOT loosened to "any
        # non-blank club" — common surnames (Silva, Wilson, Fernandes...)
        # collide across unrelated players constantly, which just buries
        # real signal in noise. Hyphen/apostrophe normalization above
        # already catches the genuine "same person, different formatting"
        # cases (e.g. Jan-Paul van Hecke) without needing that broadened.
        f_last = norm_f.split()[-1]
        db_last = norm_db.split()[-1]
        if f_last == db_last and len(f_last) > 2:
            club_match = [e for e in entries if e['club'] == fclub]
            blank_club = [e for e in entries if not e['club']]
            if club_match:
                for e in club_match:
                    if e['name'] not in seen:
                        candidates.append({**e, 'reason': 'same last name + matching club'})
                        seen.add(e['name'])
            elif blank_club:
                for e in blank_club:
                    if e['name'] not in seen:
                        candidates.append({**e, 'reason': 'same last name, existing player has no club set'})
                        seen.add(e['name'])
    return candidates


def main():
    conn = sqlite3.connect(DB_PATH)
    by_norm = load_db_players(conn)

    same_club = []
    transfers = []
    new_players = []
    ambiguous = []

    with open(PROJECTIONS_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row['Player']
            fclub = TEAM_MAP.get(row['Team'], row['Team'])
            fpos_codes = row['Position'].split(',')
            fpos = [POSITION_MAP.get(p, p) for p in fpos_codes]
            fpts = row['FPts']
            fpg = row['FP/G']
            norm = normalize(name)
            candidates = by_norm.get(norm, [])

            record = {
                'fantrax_name': name, 'fantrax_club': fclub, 'fantrax_positions': fpos,
                'fpts': fpts, 'fpg': fpg,
            }

            if len(candidates) == 1:
                db = candidates[0]
                if db['club'] == fclub:
                    same_club.append({**record, 'db_name': db['name']})
                else:
                    transfers.append({**record, 'db_name': db['name'], 'old_club': db['club']})
            elif len(candidates) > 1:
                ambiguous.append({**record, 'reason': 'multiple exact-normalized matches',
                                   'candidates': candidates})
            else:
                fuzzy = fuzzy_candidates(name, fclub, by_norm)
                if fuzzy:
                    ambiguous.append({**record, 'reason': fuzzy[0]['reason'], 'candidates': fuzzy})
                else:
                    new_players.append(record)

    conn.close()

    print(json.dumps({
        'summary': {
            'total': len(same_club) + len(transfers) + len(new_players) + len(ambiguous),
            'same_club': len(same_club),
            'transfers': len(transfers),
            'new_players': len(new_players),
            'ambiguous': len(ambiguous),
        },
        'transfers': transfers,
        'new_players': new_players,
        'ambiguous': ambiguous,
    }, indent=2))


if __name__ == '__main__':
    main()
