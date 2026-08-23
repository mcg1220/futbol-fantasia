"""
Find and fix every player whose canonical name (players.name, and every
other table that references a player by name string) doesn't match the
accented spelling WhoScored actually renders on its match pages.

Root cause (first found via Jérémy Jacquet): scraper.py takes player names
verbatim from WhoScored's page, with no accent-normalization. For most
players this already matches players.name. But for anyone whose canonical
record was seeded from a source that dropped diacritics -- empirically,
players who didn't appear in the Premier League last season, so their name
came from a different/earlier import than the rest of the pool -- every
live scrape of them creates an orphaned raw_stats row under the accented
spelling that never matches their roster/draft/players record. That
orphaned row is invisible everywhere in the app: real stats exist in the
database, but nothing shows them.

This scans this season's scraped raw_stats for player names that don't
exactly match anything in `players`, and tries to pair each one with its
un-accented counterpart already in the pool (comparing with diacritics
stripped). For every confident, unambiguous pair, it renames the
un-accented canonical spelling to the accented one WhoScored uses --
across every table that references a player by name string -- so future
scrapes of that player will already match, permanently, not just for this
gameweek.

Ambiguous matches (more than one un-accented candidate) and orphans with
no candidate at all (genuinely never added to the player pool -- a
different problem, not a spelling mismatch) are reported but never
auto-applied; those need a human to look at them.

    python3 fix_accent_mismatches.py            # dry run: report only
    python3 fix_accent_mismatches.py --apply    # write the confident pairs
"""
import argparse
import sqlite3
import unicodedata

from init_db import DB_PATH

SEASON_CUTOFF = 1983000  # matches app.py's SEASON_CUTOFF -- raw_stats has no season column

# (table, column) pairs -- every place a player is referenced by name string.
TARGETS = [
    ("players", "name"),
    ("rosters", "player_name"),
    ("draft_picks", "player_name"),
    ("raw_stats", "player_name"),
    ("transfer_pool", "player_name"),
    ("transfer_draft_picks", "player_name"),
    ("transfer_draft_picks", "dropped_player"),
    ("shortlists", "player_name"),
    ("player_trade_items", "player_name"),
    ("player_projections", "player_name"),
    ("transactions", "added_player"),
    ("transactions", "dropped_player"),
    ("waiver_claims", "add_player"),
    ("waiver_claims", "drop_player"),
]


def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


def find_pairs(conn):
    """Returns (confident_pairs, ambiguous, unmatched).
    confident_pairs: [(wrong_name, canonical_accented_name)]
    ambiguous: [(orphan_name, [candidate1, candidate2, ...])]
    unmatched: [orphan_name]  -- no accent-stripped match found at all
    """
    canonical_names = [r[0] for r in conn.execute("SELECT name FROM players")]
    canonical_set = set(canonical_names)
    stripped_to_canonical = {}
    for name in canonical_names:
        stripped_to_canonical.setdefault(strip_accents(name), []).append(name)

    orphans = [r[0] for r in conn.execute(
        "SELECT DISTINCT player_name FROM raw_stats WHERE match_id >= ? AND external=0",
        (SEASON_CUTOFF,)
    ) if r[0] not in canonical_set]

    confident_pairs, ambiguous, unmatched = [], [], []
    for orphan in orphans:
        candidates = stripped_to_canonical.get(strip_accents(orphan), [])
        # An orphan whose own stripped form is already in the canonical set
        # under its own (accented) spelling isn't un-accented -- it's some
        # other kind of mismatch (typo, different player). Only treat it as
        # a "canonical needs accents" case if the candidate is un-accented
        # relative to the orphan (i.e. stripping accents changed the orphan).
        if strip_accents(orphan) == orphan:
            continue  # orphan has no accents itself -- not this bug
        if len(candidates) == 1:
            confident_pairs.append((candidates[0], orphan))
        elif len(candidates) > 1:
            ambiguous.append((orphan, candidates))
        else:
            unmatched.append(orphan)

    return confident_pairs, ambiguous, unmatched


def rename_everywhere(conn, wrong_name, canonical_name):
    total = 0
    touched = []
    for table, col in TARGETS:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (wrong_name,)).fetchone()[0]
        if count:
            touched.append((table, col, count))
            total += count
    for table, col, _ in touched:
        conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (canonical_name, wrong_name))
    return total, touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    confident_pairs, ambiguous, unmatched = find_pairs(conn)

    if not confident_pairs and not ambiguous and not unmatched:
        print("No orphaned accented names found this season — nothing to do.")
        return

    if confident_pairs:
        print(f"Confident pairs found ({len(confident_pairs)}) — will rename un-accented -> accented:")
        for wrong, canonical in confident_pairs:
            print(f"  {wrong!r} -> {canonical!r}")
    if ambiguous:
        print(f"\nAMBIGUOUS — more than one un-accented candidate, skipped (needs a human):")
        for orphan, candidates in ambiguous:
            print(f"  {orphan!r} could match any of: {candidates}")
    if unmatched:
        print(f"\nUNMATCHED — scraped this season under an accented name with no un-accented "
              f"counterpart in `players` at all (likely never added to the player pool, a "
              f"different problem than this script fixes):")
        for orphan in unmatched:
            print(f"  {orphan!r}")

    if not confident_pairs:
        print("\nNothing safe to auto-apply. Re-run after resolving the above manually.")
        return

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write the {len(confident_pairs)} confident pair(s).")
        return

    grand_total = 0
    for wrong, canonical in confident_pairs:
        total, touched = rename_everywhere(conn, wrong, canonical)
        grand_total += total
        detail = ', '.join(f"{t}.{c}={n}" for t, c, n in touched)
        print(f"  Renamed {wrong!r} -> {canonical!r}: {total} row(s) ({detail})")
    conn.commit()
    print(f"\nApplied — {grand_total} row(s) renamed across {len(confident_pairs)} player(s).")
    conn.close()


if __name__ == '__main__':
    main()
