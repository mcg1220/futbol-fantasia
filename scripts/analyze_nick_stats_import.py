"""
Read-only analysis of Nick's supplied 2025-26 raw-stats export
(nick-supplied-data/league_draft_stats_aug2026.csv) against our own
raw_stats table. Two independent outputs, both informational only:

1. Sanity check: for every (match_id, player) our scraper and his both
   cover, do the per-column stat values agree? His scraper is what ours
   was built on top of, so this is a drift/regression check, not a
   one-directional trust exercise.
2. Backfill candidates: 2025-26 history for players who've transferred
   into the Prem from elsewhere, parsed from the doc's two name lists
   (included / explicitly-not-sourced) rather than hardcoded here, so a
   refreshed doc+CSV pair stays in sync automatically.

Does NOT write to the database. Outputs JSON for the review Artifact.

Run: python3 analyze_nick_stats_import.py > /tmp/nick_stats_diff.json
"""

import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from init_db import DB_PATH

CSV_PATH = "/Users/michaelgarcia/futbol_fantasia/nick-supplied-data/league_draft_stats_aug2026.csv"
DOC_PATH = "/Users/michaelgarcia/futbol_fantasia/nick-supplied-data/league_draft_stats_aug2026_doc"

CURRENT_PL_CLUBS = {
    'Arsenal', 'Aston Villa', 'Bournemouth', 'Brentford', 'Brighton', 'Chelsea',
    'Coventry', 'Crystal Palace', 'Everton', 'Fulham', 'Hull', 'Ipswich',
    'Leeds', 'Liverpool', 'Manchester City', 'Manchester United', 'Newcastle',
    'Nottingham Forest', 'Sunderland', 'Tottenham',
}

# CSV column -> raw_stats column, cast via float() first since some numeric
# cells are written like "6.0" rather than a bare int.
STAT_COLS = {
    'G': 'goals', 'A': 'assists', 'PKSave': 'pk_saves', 'Ylw': 'yellow_cards',
    'Red': 'red_cards', 'GLC': 'glc', 'LMT': 'lmt', 'ELG': 'elg',
    'OG': 'own_goals', 'MotM': 'motm', 'SoT': 'shots_on_target',
    'KP': 'key_passes', 'Drb': 'dribbles', 'tkl': 'tackles', 'int': 'interceptions',
    'clr': 'clearances', 'blk': 'blocked_shots', 'saves': 'saves',
    'AccCross': 'acc_crosses', 'AccLB': 'acc_long_balls',
}

MANUAL_CHAR_MAP = str.maketrans({
    'Ø': 'O', 'ø': 'o', 'Đ': 'D', 'đ': 'd', 'Þ': 'Th', 'þ': 'th',
    'Æ': 'Ae', 'æ': 'ae', 'Œ': 'Oe', 'œ': 'oe', 'ß': 'ss', 'Ł': 'L', 'ł': 'l',
})


def normalize(name):
    name = name.translate(MANUAL_CHAR_MAP)
    name = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    name = name.replace('-', ' ').replace("'", '')
    return ' '.join(name.lower().split())


def to_num(v):
    if v in (None, ''):
        return 0
    return int(float(v))


def derive_minutes(row):
    """Same derivation scripts/scraper.py's parse_incidents() uses: default
    90 (started, played the full match), overridden by sub-off/sub-on
    timestamps when present."""
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
    """Pull the included/excluded transfer-in player name lists out of the
    doc file itself, rather than hardcoding them a second time here."""
    text = open(path).read()

    def extract_names(block):
        return re.findall(r"'([^']+)'", block)

    included = []
    m = re.search(r"## Includes players transfered back.*?:\s*(.*?)\n##", text, re.S)
    if m:
        included += extract_names(m.group(1))
    m = re.search(r"## Includes following players from external leagues:\s*(.*?)\n##", text, re.S)
    if m:
        included += extract_names(m.group(1))

    excluded = []
    m = re.search(r"## Does Not include.*?:\s*(.*)", text, re.S)
    if m:
        excluded += extract_names(m.group(1))

    return included, excluded


def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    conn = sqlite3.connect(DB_PATH)
    csv_rows = load_csv(CSV_PATH)

    db_by_name = defaultdict(list)
    for r in conn.execute("SELECT name FROM players").fetchall():
        db_by_name[normalize(r[0])].append(r[0])

    # ---- 1. Sanity check over shared matches ----
    db_stats = {}
    for row in conn.execute(f"""
        SELECT match_id, player_name, club, {', '.join(STAT_COLS.values())}
        FROM raw_stats
    """).fetchall():
        match_id, player_name, club, *vals = row
        db_stats[(match_id, player_name)] = dict(zip(STAT_COLS.values(), vals))

    db_match_ids = {r[0] for r in conn.execute("SELECT DISTINCT match_id FROM raw_stats").fetchall()}

    unmatched_names = []
    clean_count = 0
    mismatches = []

    for r in csv_rows:
        match_id = int(r['matchid'])
        if match_id not in db_match_ids:
            continue  # not a shared match, handled by the backfill side instead

        csv_name = r['player']
        norm = normalize(csv_name)
        candidates = db_by_name.get(norm, [])

        # exact/normalized name match against a raw_stats row for this exact match
        db_name = None
        for cand in candidates:
            if (match_id, cand) in db_stats:
                db_name = cand
                break

        if db_name is None:
            unmatched_names.append({
                'match_id': match_id, 'csv_name': csv_name, 'csv_club': r['team'],
                'csv_date': r['date'], 'db_candidates': candidates,
            })
            continue

        db_row = db_stats[(match_id, db_name)]
        diffs = {}
        for csv_col, db_col in STAT_COLS.items():
            csv_val = to_num(r[csv_col])
            db_val = db_row[db_col]
            if csv_val != db_val:
                diffs[db_col] = {'nick': csv_val, 'ours': db_val}

        if diffs:
            mismatches.append({
                'match_id': match_id, 'player': db_name, 'csv_name': csv_name,
                'club': r['team'], 'opponent': r['opponent'], 'date': r['date'],
                'diffs': diffs,
            })
        else:
            clean_count += 1

    # ---- 2. Transfer backfill candidates ----
    included_names, excluded_names = parse_doc_lists(DOC_PATH)

    backfill_candidates = []
    unresolved = []
    for name in included_names:
        norm = normalize(name)
        candidates = db_by_name.get(norm, [])
        if len(candidates) != 1:
            unresolved.append({'doc_name': name, 'db_candidates': candidates})
            continue
        db_name = candidates[0]
        position = conn.execute("SELECT position FROM players WHERE name=?", (db_name,)).fetchone()[0]

        raw_pre_transfer_rows = [
            r for r in csv_rows
            if r['player'] == name and r['team'] not in CURRENT_PL_CLUBS
        ]
        # Nick's CSV has genuine duplicate rows for some players (same
        # match_id repeated with identical stats, confirmed by inspection --
        # e.g. Abdul Fatawu had 85 rows across only 44 unique match_ids).
        # Dedup by match_id, keeping the first occurrence, before counting.
        seen_match_ids = set()
        pre_transfer_rows = []
        duplicate_row_count = 0
        for r in raw_pre_transfer_rows:
            if r['matchid'] in seen_match_ids:
                duplicate_row_count += 1
                continue
            seen_match_ids.add(r['matchid'])
            pre_transfer_rows.append(r)

        if not pre_transfer_rows:
            continue

        total_minutes = sum(derive_minutes(r) for r in pre_transfer_rows)
        clubs_played_for = sorted({r['team'] for r in pre_transfer_rows})

        backfill_candidates.append({
            'doc_name': name, 'db_name': db_name, 'position': position,
            'matches': len(pre_transfer_rows), 'total_minutes_approx': total_minutes,
            'prior_clubs': clubs_played_for,
            'gc_suppressed': position in ('DEF', 'GK'),
            'duplicate_rows_skipped': duplicate_row_count,
        })

    conn.close()

    print(json.dumps({
        'summary': {
            'shared_matches': len(db_match_ids),
            'clean_rows': clean_count,
            'mismatches': len(mismatches),
            'unmatched_names': len(unmatched_names),
            'backfill_candidates': len(backfill_candidates),
            'unresolved_names': len(unresolved),
            'doc_excluded_count': len(excluded_names),
        },
        'mismatches': mismatches,
        'unmatched_names': unmatched_names,
        'backfill_candidates': backfill_candidates,
        'unresolved_names': unresolved,
        'doc_excluded_names': excluded_names,
    }, indent=2))


if __name__ == '__main__':
    main()
