"""
Fútbol de Fantasía - Badge Fetcher
Fetches PL team badge URLs from TheSportsDB (free, no API key required).
Saves results to data/badges.json.

Usage (run once from futbol_fantasia/ folder):
    python fetch_badges.py
"""

import json
import time
import os
import urllib.request
import urllib.parse

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'badges.json')

# Maps the team names stored in our DB → TheSportsDB search term
TEAM_SEARCH_MAP = {
    "Arsenal":            "Arsenal",
    "Aston Villa":        "Aston Villa",
    "Brentford":          "Brentford",
    "Brighton":           "Brighton & Hove Albion",
    "Bournemouth":        "AFC Bournemouth",
    "Chelsea":            "Chelsea",
    "Coventry":           "Coventry City",
    "Crystal Palace":     "Crystal Palace",
    "Everton":            "Everton",
    "Fulham":             "Fulham",
    "Hull":               "Hull City",
    "Ipswich":            "Ipswich Town",
    "Leeds":              "Leeds United",
    "Liverpool":          "Liverpool",
    "Manchester City":    "Manchester City",
    "Manchester United":  "Manchester United",
    "Newcastle":          "Newcastle United",
    "Nottingham Forest":  "Nottingham Forest",
    "Sunderland":         "Sunderland",
    "Tottenham":          "Tottenham Hotspur",
}

BASE_URL = "https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t="

badges = {}

print("Fetching badge URLs from TheSportsDB...\n")

for db_name, search_name in TEAM_SEARCH_MAP.items():
    url = BASE_URL + urllib.parse.quote(search_name)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        teams = data.get("teams")
        if not teams:
            print(f"  ⚠️  {db_name}: no results for '{search_name}'")
            continue

        # Pick the first result that looks like the right team
        team = teams[0]
        badge_url = team.get("strBadge") or team.get("strTeamBadge")

        if badge_url:
            # Append /tiny for small thumbnails (TheSportsDB feature)
            badges[db_name] = badge_url + "/tiny"
            print(f"  ✅ {db_name}: {badge_url[:60]}...")
        else:
            print(f"  ⚠️  {db_name}: badge URL missing in response")

    except Exception as e:
        print(f"  ❌ {db_name}: {e}")

    time.sleep(0.5)  # be polite to the free API

# Save
with open(OUTPUT_PATH, 'w') as f:
    json.dump(badges, f, indent=2)

print(f"\nSaved {len(badges)} badge URLs to {OUTPUT_PATH}")
