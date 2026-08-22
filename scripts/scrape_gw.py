"""
Fútbol de Fantasía - Gameweek Scraper
Scrapes all matches for a given gameweek from WhoScored.

Usage:
    python3 scrape_gw.py --gw 5
    python3 scrape_gw.py --gw 5 --rescrape
    python3 scrape_gw.py --gw 5 --compare
    python3 scrape_gw.py --gw 1 --gw_end 34    # scrape a range of GWs
    python3 scrape_gw.py --gw 1 --season 2025-26
"""

import sqlite3
import argparse
import os
import signal
import subprocess
import sys
import time
import json
from types import SimpleNamespace
from init_db import DB_PATH


def run_match_scrape(cmd, timeout):
    """
    Runs a single match's scraper.py with a hard wall-clock timeout, killing
    its whole process group (not just the scraper.py pid) if it's exceeded.

    Plain subprocess.run(..., timeout=...) only kills the direct child on
    timeout — but scraper.py launches a Playwright-driven Chromium browser
    as a grandchild process, which does NOT reliably die with its parent.
    A match that hangs (WhoScored slow to respond, a selector that never
    resolves) left that orphaned Chromium running indefinitely, and those
    piled up across a scrape run until the container ran out of memory and
    got OOM-killed by the host — which is what took the site down. Launching
    scraper.py in its own session (preexec_fn=os.setsid) means a timeout can
    os.killpg() the whole tree, browser included, exactly like the outer
    scrape_gw.py process already does for its own cancel button.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        preexec_fn=os.setsid,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)

    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=proc.returncode)


def get_match_ids_for_gw(gw_number, season='2026-27', match_ids=None):
    """Return match IDs for a given gameweek and season — all of them, or
    just the subset in match_ids if given (lets a caller re-scrape only the
    fixture(s) they care about instead of the whole gameweek)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = """
        SELECT f.match_id, f.home_club, f.away_club
        FROM fixtures f
        JOIN gameweeks g ON f.gw_id = g.id
        WHERE g.gw_number = ? AND f.season = ?
    """
    params = [gw_number, season]
    if match_ids:
        query += f" AND f.match_id IN ({','.join('?' * len(match_ids))})"
        params += list(match_ids)
    query += " ORDER BY f.match_id"
    rows = c.execute(query, params).fetchall()
    conn.close()
    return rows


def scrape_gw(gw_number, rescrape=False, compare=False, delay=4, season='2026-27', match_ids=None):
    """Scrape matches for a gameweek — all of them, or just match_ids if given."""
    matches = get_match_ids_for_gw(gw_number, season, match_ids=match_ids)

    if not matches:
        print(f"No matches found for GW{gw_number} ({season}) matching the given selection. Is the DB seeded?")
        return

    print(f"\n{'='*50}")
    print(f"GW{gw_number} — {len(matches)} matches ({season})")
    print(f"{'='*50}")

    results = {"perfect": [], "discrepancies": [], "errors": [], "tab_failures": []}

    for i, (match_id, home_club, away_club) in enumerate(matches, start=1):
        print(f"\n[{home_club} vs {away_club}] match_id={match_id}")
        print(f"PROGRESS:{json.dumps({'gw': gw_number, 'match_index': i, 'match_total': len(matches), 'home': home_club, 'away': away_club, 'stage': 'scraping'})}", flush=True)

        cmd = [sys.executable, "scraper.py", "--match_id", str(match_id), "--gw", str(gw_number)]
        if rescrape:
            cmd.append("--rescrape")
        if compare:
            cmd.append("--compare")

        try:
            result = run_match_scrape(cmd, timeout=120)
            output = result.stdout

            # Always surface tab-load failure warnings, regardless of which
            # branch below fires — these were previously getting silently
            # dropped since they occur before the "Comparing scraped" marker
            for line in output.splitlines():
                if "TAB LOAD FAILURES" in line or "FAILED to load" in line:
                    print(f"  {line.strip()}")
                    if "TAB LOAD FAILURES" in line:
                        results["tab_failures"].append(f"{home_club} vs {away_club}: {line.strip()}")

            if "SCRAPE FAILED" in output:
                results["errors"].append(f"{home_club} vs {away_club}: 0 players captured — match likely not played yet")
                print(f"  ❌ SCRAPE FAILED — 0 players captured (match likely not played yet)")
                outcome = "error"
            elif "✅ Perfect match!" in output:
                results["perfect"].append(f"{home_club} vs {away_club}")
                print(f"  ✅ Perfect match!")
                outcome = "perfect"
            elif "discrepancies found" in output:
                in_compare = False
                for line in output.splitlines():
                    if "Comparing scraped" in line:
                        in_compare = True
                        continue
                    if in_compare and line.strip() and not any(x in line for x in ["Scraping:", "Loading", "Teams:", "Score:", "Scraping ", "Saves:", "Scraped"]):
                        print(f"  {line.strip()}")
                    if "discrepancies found" in line:
                        results["discrepancies"].append(f"{home_club} vs {away_club}: {line.strip()}")
                outcome = "discrepancy"
            elif result.returncode != 0:
                results["errors"].append(f"{home_club} vs {away_club}: scraper error")
                print(f"  ❌ Error")
                if result.stderr:
                    print(f"     {result.stderr[-800:]}")  # tail of stderr has the actual exception
                outcome = "error"
            else:
                for line in output.splitlines():
                    if "Saved" in line or "Warning" in line:
                        print(f"  {line.strip()}")
                outcome = "saved"

        except subprocess.TimeoutExpired:
            results["errors"].append(f"{home_club} vs {away_club}: timeout")
            print(f"  ❌ Timeout")
            outcome = "timeout"

        print(f"PROGRESS:{json.dumps({'gw': gw_number, 'match_index': i, 'match_total': len(matches), 'home': home_club, 'away': away_club, 'stage': 'done', 'outcome': outcome})}", flush=True)

        time.sleep(delay)

    # Summary
    print(f"\n{'='*50}")
    print(f"GW{gw_number} Summary ({season})")
    print(f"{'='*50}")
    print(f"  ✅ Perfect:       {len(results['perfect'])}/{len(matches)}")
    print(f"  ⚠️  Discrepancies: {len(results['discrepancies'])}/{len(matches)}")
    print(f"  ❌ Errors:        {len(results['errors'])}/{len(matches)}")
    print(f"  ⚠️⚠️  Tab failures: {len(results['tab_failures'])}")

    if results["discrepancies"]:
        print("\n  Discrepancy details:")
        for d in results["discrepancies"]:
            print(f"    {d}")
    if results["errors"]:
        print("\n  Error details:")
        for e in results["errors"]:
            print(f"    {e}")
    if results["tab_failures"]:
        print("\n  Tab failure details (these matches likely need --rescrape):")
        for t in results["tab_failures"]:
            print(f"    {t}")

    results["gw"] = gw_number
    results["season"] = season
    results["total_fixtures"] = len(matches)
    print(f"RESULT_JSON:{json.dumps(results)}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape all matches for a gameweek.")
    parser.add_argument("--gw",      type=int, required=True,    help="Gameweek number to scrape")
    parser.add_argument("--gw_end",  type=int, default=None,     help="End gameweek for range scraping")
    parser.add_argument("--season",  type=str, default='2026-27',help="Season to scrape (default: 2026-27)")
    parser.add_argument("--rescrape",action="store_true",        help="Re-scrape and overwrite existing data")
    parser.add_argument("--compare", action="store_true",        help="Compare against DB without saving")
    parser.add_argument("--delay",   type=int, default=4,        help="Seconds between matches (default: 4)")
    parser.add_argument("--match_ids", type=str, default=None,
                         help="Comma-separated match_ids to scrape instead of the whole gameweek, e.g. 1983546,1983548")
    args = parser.parse_args()

    match_ids = [int(m) for m in args.match_ids.split(',')] if args.match_ids else None

    any_errors = False

    if args.gw_end:
        for gw in range(args.gw, args.gw_end + 1):
            r = scrape_gw(gw, rescrape=args.rescrape, compare=args.compare,
                          delay=args.delay, season=args.season, match_ids=match_ids)
            if r and r["errors"]:
                any_errors = True
            print(f"\nWaiting 10 seconds before next GW...")
            time.sleep(10)
    else:
        r = scrape_gw(args.gw, rescrape=args.rescrape, compare=args.compare,
                      delay=args.delay, season=args.season, match_ids=match_ids)
        if r and r["errors"]:
            any_errors = True

    sys.exit(1 if any_errors else 0)
