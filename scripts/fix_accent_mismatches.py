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

Not every mismatch is about accents, though -- e.g. WhoScored rendering
"Josh King" while the pool has "Joshua King" is a nickname mismatch, not a
diacritic one, so accent-stripping can't find it automatically. MANUAL_PAIRS
below is a small human-curated list of exactly these non-accent cases,
found by spot-checking a player's stats when someone reports them missing.
They're applied with the same rename-everywhere logic and the same safety
checks (skipped if already fixed, or if renaming would collide) -- add to
this list as new non-accent mismatches turn up.

Since new matches keep getting scraped throughout a gameweek (and beyond,
via manual re-scrapes), re-running this script periodically -- not just
once -- is expected and safe; it only ever acts on names that still need
fixing.

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

# Non-accent mismatches (nicknames, etc.) found by hand -- see module
# docstring. (name currently in players/db, name WhoScored actually uses --
# renamed FROM the first TO the second, same direction as the automatic
# accent fix above: the pool always converges on WhoScored's spelling, so
# future scrapes keep matching.)
MANUAL_PAIRS = [
    ("Joshua King", "Josh King"),
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

    # Fold in the manually-curated non-accent pairs, skipping any that are
    # already fixed (wrong_name no longer appears anywhere) -- keeps re-runs
    # a no-op. rename_everywhere/merge_duplicate_player below already handle
    # a collision with an existing row under the target name, if there is one.
    for wrong_name, canonical_name in MANUAL_PAIRS:
        still_present = any(
            conn.execute(f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1", (wrong_name,)).fetchone()
            for table, col in TARGETS
        )
        if not still_present:
            continue
        confident_pairs.append((wrong_name, canonical_name))

    return confident_pairs, ambiguous, unmatched


def find_duplicate_player_ids(conn, wrong_name, canonical_name):
    """(wrong_id, canonical_id) if both names already exist as SEPARATE
    players.id rows -- a genuine duplicate player record (e.g. the scraper
    auto-creating a stub the first time it saw someone under a name that
    didn't match the existing pool entry), not a simple rename. None if
    there's no such collision."""
    wrong_row = conn.execute("SELECT id FROM players WHERE name=?", (wrong_name,)).fetchone()
    canonical_row = conn.execute("SELECT id FROM players WHERE name=?", (canonical_name,)).fetchone()
    if wrong_row and canonical_row and wrong_row['id'] != canonical_row['id']:
        return wrong_row['id'], canonical_row['id']
    return None


def rename_everywhere(conn, wrong_name, canonical_name, skip_players_table=False):
    total = 0
    touched = []
    targets = [(t, c) for t, c in TARGETS if not (skip_players_table and t == 'players')]
    for table, col in targets:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (wrong_name,)).fetchone()[0]
        if count:
            touched.append((table, col, count))
            total += count
    for table, col, _ in touched:
        conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (canonical_name, wrong_name))
    return total, touched


def merge_duplicate_player(conn, wrong_id, canonical_id):
    """Handles the duplicate-player-record case, called AFTER
    rename_everywhere(..., skip_players_table=True) has already redirected
    every name-string reference from wrong_name to canonical_name. Safe to
    always delete the stub players/eligibility rows at that point: the
    schema has exactly one FK to players.id (player_eligibility), and
    every other table references a player by name string, not id -- so
    once the strings are migrated, the stub row has nothing left pointing
    to it. Migrates the stub's whoscored_id onto the canonical row first,
    if the canonical row doesn't already have one."""
    stub = conn.execute("SELECT whoscored_id FROM players WHERE id=?", (wrong_id,)).fetchone()
    canonical = conn.execute("SELECT whoscored_id FROM players WHERE id=?", (canonical_id,)).fetchone()
    if stub['whoscored_id'] is not None and canonical['whoscored_id'] is None:
        conn.execute("UPDATE players SET whoscored_id=? WHERE id=?", (stub['whoscored_id'], canonical_id))

    conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (wrong_id,))
    conn.execute("DELETE FROM players WHERE id=?", (wrong_id,))


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
        print(f"Confident pairs found ({len(confident_pairs)}):")
        for wrong, canonical in confident_pairs:
            dup = find_duplicate_player_ids(conn, wrong, canonical)
            if dup:
                print(f"  {wrong!r} -> {canonical!r}  (DUPLICATE PLAYER RECORDS -- will merge, not just rename)")
            else:
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
        dup = find_duplicate_player_ids(conn, wrong, canonical)
        if dup:
            wrong_id, canonical_id = dup
            total, touched = rename_everywhere(conn, wrong, canonical, skip_players_table=True)
            merge_duplicate_player(conn, wrong_id, canonical_id)
            grand_total += total
            detail = ', '.join(f"{t}.{c}={n}" for t, c, n in touched) or "no other tables referenced it"
            print(f"  Merged {wrong!r} (id={wrong_id}) into {canonical!r} (id={canonical_id}): "
                  f"{total} row(s) renamed ({detail}), stub player record removed")
        else:
            total, touched = rename_everywhere(conn, wrong, canonical)
            grand_total += total
            detail = ', '.join(f"{t}.{c}={n}" for t, c, n in touched)
            print(f"  Renamed {wrong!r} -> {canonical!r}: {total} row(s) ({detail})")
    conn.commit()
    print(f"\nApplied — {grand_total} row(s) renamed across {len(confident_pairs)} player(s).")
    conn.close()


if __name__ == '__main__':
    main()
