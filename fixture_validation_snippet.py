# Add this function to scrape_fixtures.py, and call it at the end of scrape_fixtures()

def validate_gw_counts(conn, season, expected_per_gw=10):
    """
    Sanity check: every GW should have exactly `expected_per_gw` fixtures.
    Prints a warning for any GW that doesn't match — catches mis-assignment
    bugs (e.g. team-name matching errors) before they cause scoring issues.
    """
    c = conn.cursor()
    rows = c.execute("""
        SELECT g.gw_number, COUNT(*) as cnt
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season = ?
        GROUP BY g.gw_number
        ORDER BY g.gw_number
    """, (season,)).fetchall()

    problems = [(gw, cnt) for gw, cnt in rows if cnt != expected_per_gw]

    if problems:
        print(f"\n⚠️  WARNING: {len(problems)} gameweek(s) don't have exactly "
              f"{expected_per_gw} fixtures for {season}:")
        for gw, cnt in problems:
            print(f"    GW{gw}: {cnt} fixtures (expected {expected_per_gw})")
        print("    This usually means a fixture was matched to the wrong GW.")
        print("    Investigate before trusting scoring data for these GWs.\n")
    else:
        print(f"\n✅ All GWs for {season} have exactly {expected_per_gw} fixtures.\n")
