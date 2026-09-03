"""
One-off diagnostic: find scraped player names that don't match anyone in
the `players` table.

Why this matters: the scraper writes raw_stats keyed by the exact name
string it reads off WhoScored, with no lookup against `players` at write
time. Scoring then matches a roster to raw_stats purely by exact
player_name string. So a real player who transfers clubs is fine (their
name doesn't change) -- but a name that was manually typed into `players`
via "+ Add Brand-New Player" (a genuinely new-to-Premier-League signing,
not an existing player switching clubs) can silently mismatch WhoScored's
actual spelling (accents, suffixes, short vs. full name). When that
happens, the scraped stats land under a name nobody's roster matches --
that player just shows 0 fantasy points, with no error anywhere.

This is a read-only check -- run it after any scrape/rescrape to catch
that before it costs someone points. Reports two tiers:
  - UNMATCHED, no close name found: most likely a real problem, or a
    player nobody has added to `players` yet.
  - UNMATCHED, but a close match exists in `players`: probably the same
    person with a spelling difference (accent, hyphen, etc.) -- the
    suggested fix is normally to rename the `players` row to match
    WhoScored's exact spelling (raw_stats reflects the real scrape output,
    so `players` should conform to it, not the other way around).

Only flags names with real minutes played -- an unused substitute
(minutes_played=0) scored nothing regardless, so a name mismatch there
costs no one any points.

Usage:
    python3 check_unmatched_player_names.py [--gw N] [--season 2026-27]
"""
import argparse
import difflib
import sqlite3
import unicodedata

from init_db import DB_PATH

DEFAULT_SEASON = '2026-27'


def normalize(name):
    """Strip accents/diacritics and lowercase, for a looser secondary
    comparison than the exact string match scoring itself relies on."""
    decomposed = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gw', type=int, help='Limit to one gameweek (default: all gws scraped this season)')
    parser.add_argument('--season', default=DEFAULT_SEASON)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    where = "f.season=? AND rs.minutes_played > 0"
    params = [args.season]
    if args.gw:
        where += " AND rs.gw_number=?"
        params.append(args.gw)

    scraped_rows = c.execute(f"""
        SELECT DISTINCT rs.player_name, rs.club, rs.gw_number
        FROM raw_stats rs JOIN fixtures f ON f.match_id = rs.match_id
        WHERE {where}
        ORDER BY rs.gw_number, rs.player_name
    """, params).fetchall()

    known_names = [r[0] for r in c.execute("SELECT name FROM players").fetchall()]
    known_set = set(known_names)
    normalized_lookup = {}
    for n in known_names:
        normalized_lookup.setdefault(normalize(n), []).append(n)

    unmatched_no_hint = []
    unmatched_with_hint = []
    for row in scraped_rows:
        name = row['player_name']
        if name in known_set:
            continue
        norm = normalize(name)
        if norm in normalized_lookup:
            unmatched_with_hint.append((row, normalized_lookup[norm]))
            continue
        close = difflib.get_close_matches(name, known_names, n=3, cutoff=0.75)
        if close:
            unmatched_with_hint.append((row, close))
        else:
            unmatched_no_hint.append(row)

    print(f"Checked {len(scraped_rows)} distinct (player, gw) rows with real minutes "
          f"for {args.season}" + (f" GW{args.gw}" if args.gw else " (all scraped gws)") + ".\n")

    if unmatched_with_hint:
        print(f"⚠️  {len(unmatched_with_hint)} name(s) unmatched but a likely candidate exists in `players` "
              f"(probably a spelling/accent difference -- fix by renaming the `players` row to WhoScored's spelling):")
        for row, candidates in unmatched_with_hint:
            print(f"  GW{row['gw_number']}  \"{row['player_name']}\" ({row['club']})  →  did you mean: {', '.join(candidates)}?")
        print()

    if unmatched_no_hint:
        print(f"❌ {len(unmatched_no_hint)} name(s) unmatched with no close candidate "
              f"(likely a real player missing from `players`, or a bigger spelling gap):")
        for row in unmatched_no_hint:
            print(f"  GW{row['gw_number']}  \"{row['player_name']}\" ({row['club']})")
        print()

    if not unmatched_with_hint and not unmatched_no_hint:
        print("✅ Every scraped name with real minutes matches an existing player. Nothing to fix.")

    conn.close()


if __name__ == '__main__':
    main()
