"""
Scrape WhoScored positions for players who have stats but no `players` row
(found by report_missing_from_pool.py). Runs LOCALLY -- WhoScored blocks
Render's IP. Touches no database: it writes new_player_positions.json, which
the production fix script (fix_pool_gaps.py) reads.

For each name: WhoScored search -> the result whose club matches the club he
plays for in our stats -> his profile page's "Positions:" line (first listed
group = primary). A fresh browser per page load, with pauses, because
WhoScored's Cloudflare challenges a long-lived headless session after one or
two requests (the match scraper launches fresh per match for the same reason).

    python3 scrape_new_player_positions.py --out new_player_positions.json
    python3 scrape_new_player_positions.py --out x.json --only "Hugo Larsson"
"""
import argparse
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

SITE = "https://futbol-fantasia.onrender.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

NAMES = [
    "David Affengruber", "Alysson", "Lucas Gourna-Douath", "Ainsley Maitland-Niles",
    "Mohamed-Ali Cho", "Yann Gboho", "Jules Ahoka", "Honest Ahanor", "Hugo Larsson",
    "Dayann Méthalie", "Darío Osorio", "Melvin Bard", "Robinio Vaz", "Jaouen Hadjam",
    "Allan", "Abdul Fatawu Issahaku", "Manuel Ángel", "Ferdi Kadıoğlu",
    "Jean-Mattéo Bahoya", "Juan Angulo", "Dino Kaiser", "Luca Gunter", "Michael Zetterer",
    # flagged as possible spelling twins, but actually different people
    "Emersonn", "Lucas Herrington", "Kyran Thompson", "Michael Mills", "Younes Ibrahim",
]

# IDs read straight off match pages where these players appeared (search
# returns other people with the same name); tried before search results.
KNOWN_IDS = {"Allan": 521094, "Manuel Ángel": 606004, "Dino Kaiser": 661233}


def norm(s):
    d = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().replace('ı', 'i').strip()


def classify(label):
    label = label.lower()
    if 'goalkeeper' in label:
        return 'GK'
    if 'back' in label or 'defender' in label:
        return 'DEF'
    if 'midfielder' in label:
        return 'MID'
    if 'forward' in label or 'striker' in label:
        return 'FW'
    return None


def split_top_level_commas(text):
    groups, depth, cur = [], 0, ''
    for ch in text:
        depth += (ch == '(') - (ch == ')')
        if ch == ',' and depth == 0:
            groups.append(cur.strip())
            cur = ''
        else:
            cur += ch
    return groups + ([cur.strip()] if cur.strip() else [])


def club_of(name):
    url = f"{SITE}/api/player_history?name={urllib.parse.quote(name)}"
    h = json.load(urllib.request.urlopen(url, timeout=60)).get('history', [])
    h = [r for r in h if r.get('club')]
    return max(h, key=lambda r: r['gw_number'])['club'] if h else None


def fetch(p, url, wait=6):
    """Load one page in a fresh browser; return (title, body text, links)."""
    b = p.chromium.launch(headless=True)
    try:
        page = b.new_context(user_agent=UA).new_page()
        page.route("**/*.{png,jpg,jpeg,gif,woff,woff2}", lambda r: r.abort())
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(wait)
        links = page.eval_on_selector_all(
            "a[href*='/players/']",
            "els => els.map(e => [e.getAttribute('href'), e.innerText.trim(),"
            " e.closest('tr') ? e.closest('tr').innerText.replace(/\\s+/g, ' ') : ''])")
        return page.title(), page.inner_text("body"), links
    finally:
        b.close()


def club_words(club):
    # "Manchester City" -> {"manchester", "city"}; WhoScored may say "Man City"
    w = set(norm(club).split()) - {'fc', 'afc'}
    return w | ({'man'} if 'manchester' in w else set())


def find_candidates(p, name, club):
    cands, note = search(p, name, name, club)
    if not cands and len(name.split()) > 1:
        # hyphenated/accented full names often return nothing; surname alone usually works
        time.sleep(4)
        cands, note = search(p, name.split()[-1], name, club)
    return cands, note


def search(p, query, name, club):
    title, _, links = fetch(p, "https://www.whoscored.com/search/?t=" + urllib.parse.quote(query))
    if 'Cloudflare' in title:
        return [], 'blocked by Cloudflare on search'
    cands = []
    for href, text, row in links:
        m = re.search(r'/players/(\d+)/show/', href or '')
        if m:
            cands.append((int(m.group(1)), text, row))
    if not cands:
        return [], 'no search results'
    cw = club_words(club or '')
    by_club = [c for c in cands if cw and cw & set(norm(c[2]).split())]
    exact = [c for c in (by_club or cands) if norm(c[1]) == norm(name)]
    ordered = exact + [c for c in by_club if c not in exact] + [c for c in cands if c not in exact + by_club]
    return [c[0] for c in ordered[:4]], f"{len(cands)} search result(s)"


def profile_positions(p, wsid, club=None):
    """Positions from a WhoScored profile, first listed = primary. With a
    club, refuses a profile whose Current Team doesn't match (search can
    return other people with the same name); pass None when the ID came off
    a match page and is already known to be right."""
    title, body, _ = fetch(p, f"https://www.whoscored.com/players/{wsid}/show/")
    if 'Cloudflare' in title:
        return None, [], 'blocked by Cloudflare on profile'
    if club is not None:
        team = re.search(r'Current Team:(.*?)(?:Shirt Number|Age:|Height:|Nationality:|Positions:)', body, re.S)
        team = ' '.join(team.group(1).split()) if team else ''
        if not (club_words(club) & set(norm(team).split())):
            return None, [], f'wrong player (current team {team!r})'
    m = re.search(r'Positions:[^\S\n]*([^\n]+)', body)
    if not m:
        return None, [], 'no Positions line on profile'
    raw = m.group(1).strip()
    order = []
    for g in split_top_level_commas(raw):
        pos = classify(re.sub(r'\(.*?\)', '', g))
        if pos and pos not in order:
            order.append(pos)
    return (order[0] if order else None), order, f"raw: {raw}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--only', action='append')
    ap.add_argument('--pause', type=float, default=6)
    args = ap.parse_args()
    names = args.only or NAMES

    results = {}
    with sync_playwright() as p:
        for i, n in enumerate(names, 1):
            club = club_of(n)
            info = {'club': club, 'whoscored_id': None, 'primary': None, 'positions': []}
            cands, note = find_candidates(p, n, club)
            if n in KNOWN_IDS:
                cands = [KNOWN_IDS[n]] + [c for c in cands if c != KNOWN_IDS[n]]
            info['note'] = note
            time.sleep(args.pause)
            wsid = None
            for cand in cands:
                primary, order, note = profile_positions(p, cand, club)
                info['note'] = note
                time.sleep(args.pause)
                if primary:
                    wsid = cand
                    info.update(whoscored_id=cand, primary=primary, positions=order)
                    break
            results[n] = info
            print(f"[{i}/{len(names)}] {n:24} {str(club):16} {str(info['primary']):4} "
                  f"{info['positions']}  ws={wsid}  {info['note']}", flush=True)

    try:  # merge into an existing results file so re-runs of misses keep earlier finds
        with open(args.out, encoding='utf-8') as f:
            results = {**json.load(f), **results}
    except (FileNotFoundError, ValueError):
        pass
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    ok = sum(1 for v in results.values() if v['primary'])
    print(f"\n{ok}/{len(results)} positions found. Wrote {args.out}")


if __name__ == '__main__':
    main()
