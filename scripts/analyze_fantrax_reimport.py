"""
Read-only analysis: cross-reference a fresh Fantrax projected-stats export
against our players table to classify every row as same-club, a transfer, a
genuinely new player, or ambiguous (needs a human call) -- plus, unlike the
original one-shot import script, flag players who dropped OUT of the export
entirely (e.g. transferred outside the Premier League).

Does NOT write to the database. Outputs JSON for the review Artifact.

Run: python3 analyze_fantrax_reimport.py > /tmp/fantrax_reimport_diff.json
"""

import csv
import json
import os
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from init_db import DB_PATH

CSV_PATH = os.environ.get(
    'FANTRAX_CSV_PATH',
    "/Users/michaelgarcia/futbol_fantasia/fantrax schedule and projections/"
    "Fantrax-Players-Fantasy Futbol - Updated 2026-2027 Projections.csv"
)

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
    didn't match exactly even after normalization -- e.g. Fantrax listing
    just 'Alisson' for our 'Alisson Becker'."""
    norm_f = normalize(fantrax_name)
    f_tokens = set(norm_f.split())
    candidates = []
    seen = set()
    for norm_db, entries in by_norm.items():
        db_tokens = set(norm_db.split())
        if not f_tokens or not db_tokens:
            continue
        if f_tokens <= db_tokens or db_tokens <= f_tokens:
            for e in entries:
                if e['name'] not in seen:
                    candidates.append({**e, 'reason': 'partial name match'})
                    seen.add(e['name'])
            continue
        f_last = norm_f.split()[-1]
        db_last = norm_db.split()[-1]
        if f_last == db_last and len(f_last) > 2:
            club_match = [e for e in entries if e['club'] == fclub]
            blank_club = [e for e in entries if not e['club']]
            # Any OTHER PL club is still a same-last-name candidate -- this is
            # exactly the shape of a real transfer plus a first-name spelling
            # difference (e.g. "Andy Robertson" (Liverpool) -> Fantrax's
            # "Andrew Robertson" (Tottenham)). Lower precision than an exact
            # club match, so it's tagged separately and always routed to
            # human review, never auto-applied -- common surnames collide
            # across unrelated players constantly (see e.g. every "Silva").
            other_club = [e for e in entries if e['club'] and e['club'] != fclub]
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
            elif other_club:
                for e in other_club:
                    if e['name'] not in seen:
                        candidates.append({**e, 'reason': 'same last name, different club -- possible transfer + name spelling difference'})
                        seen.add(e['name'])
    return candidates


def main():
    conn = sqlite3.connect(DB_PATH)
    by_norm = load_db_players(conn)

    same_club = []
    transfers = []
    new_players = []
    ambiguous = []

    fantrax_norm_names = set()

    with open(CSV_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row['Player']
            fclub = TEAM_MAP.get(row['Team'], row['Team'])
            fpos_codes = row['Position'].split(',')
            fpos = [POSITION_MAP.get(p, p) for p in fpos_codes]
            fpts_raw = (row['FPts'] or '').strip()
            fpg = row['FP/G']
            norm = normalize(name)
            fantrax_norm_names.add(norm)
            candidates = by_norm.get(norm, [])

            try:
                fpts_val = float(fpts_raw) if fpts_raw else 0.0
            except ValueError:
                fpts_val = 0.0

            record = {
                'fantrax_name': name, 'fantrax_club': fclub, 'fantrax_positions': fpos,
                'fpts': fpts_raw, 'fpg': fpg,
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
                elif fpts_val < 1.0:
                    # Last import's known-junk entry ("Aidan Dausch", excluded
                    # by a hardcoded name at the time) had a near-zero FPts
                    # value too, not a literal 0 -- so a strict `<= 0` check
                    # would have let it straight through again. Anything
                    # under a full point needs a look either way.
                    ambiguous.append({**record, 'reason': 'near-zero projection -- confirm before adding',
                                       'candidates': []})
                else:
                    new_players.append(record)

    # Players who dropped out of the export entirely: currently in the
    # draftable pool (club is a real PL club) but nowhere in the new CSV.
    # Enrich with their last known projection so real transfers-out (had a
    # meaningful projection, now vanished) can be told apart from deep-bench
    # players Fantrax simply never bothered projecting in the first place.
    prior_proj = {row[0]: row[1] for row in conn.execute(
        "SELECT player_name, proj_total FROM player_projections WHERE season='2026-27'"
    ).fetchall()}

    # Any DB player already surfaced as a HIGH-CONFIDENCE candidate in the
    # ambiguous bucket (e.g. "Danny Ballard" fuzzy-matching to our "Dan
    # Ballard") would ALSO look "removed" here, since their normalized name
    # doesn't match the differently-formatted Fantrax name either -- but
    # that's just the same name-formatting mismatch showing up twice, not a
    # real disappearance. Resolving the ambiguous entry (in either direction)
    # makes the corresponding "removed" flag moot, so drop the overlap here
    # rather than showing the same player as two different kinds of alarming.
    #
    # Deliberately EXCLUDES the low-confidence "different club" surname-
    # collision reason: a DB player merely surfacing as *someone else's*
    # fuzzy candidate there (e.g. Bernardo Silva turning up as a guess for an
    # unrelated new Bournemouth player "Antonio Silva") says nothing about
    # whether Bernardo Silva himself is still in the export -- suppressing
    # his own real removal flag on that basis nearly hid a genuine
    # transfer-out.
    low_confidence_overlap_reason = (
        'same last name, different club -- possible transfer + name spelling difference'
    )
    ambiguous_candidate_names = {
        c['name'] for r in ambiguous for c in r.get('candidates', [])
        if c.get('reason') != low_confidence_overlap_reason
    }

    removed = []
    for norm_db, entries in by_norm.items():
        if norm_db in fantrax_norm_names:
            continue
        for e in entries:
            if e['club'] in TEAM_MAP.values() and e['name'] not in ambiguous_candidate_names:
                removed.append({
                    'db_name': e['name'], 'last_club': e['club'], 'position': e['position'],
                    'prior_proj_total': prior_proj.get(e['name']),
                })
    removed.sort(key=lambda r: r['prior_proj_total'] or 0, reverse=True)

    # Tag each ambiguous row's confidence: a single fuzzy candidate with a
    # precise reason (not the loose "no club set" fallback) is very likely
    # correct -- surfaced separately in the review UI for a fast bulk-confirm
    # instead of forcing individual attention on every one of them.
    for r in ambiguous:
        cands = r.get('candidates', [])
        low_precision_reasons = (
            'same last name, existing player has no club set',
            'same last name, different club -- possible transfer + name spelling difference',
        )
        r['confidence'] = 'high' if len(cands) == 1 and r['reason'] not in low_precision_reasons else 'low'

    conn.close()

    print(json.dumps({
        'summary': {
            'total': len(same_club) + len(transfers) + len(new_players) + len(ambiguous),
            'same_club': len(same_club),
            'transfers': len(transfers),
            'new_players': len(new_players),
            'ambiguous': len(ambiguous),
            'removed': len(removed),
        },
        'same_club': same_club,
        'transfers': transfers,
        'new_players': new_players,
        'ambiguous': ambiguous,
        'removed': removed,
    }, indent=2))


if __name__ == '__main__':
    main()
