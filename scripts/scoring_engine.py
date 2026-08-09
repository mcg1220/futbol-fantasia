"""
Fútbol de Fantasía - Scoring Engine
Calculates fantasy points for a player in a given gameweek.

Rules:
- Points applied per position restrictions in scoring_config
- Minutes: 1pt for playing 1-60 min, 2pt for 61+ min
- Clean sheet: DEF/GK only, requires 60+ minutes played
- Goals conceded (DEF): -0.5 per goal regardless of sub timing
- Goals conceded (GK): -1 per goal regardless of sub timing
- Goals conceded = opponent goals scored + own team's own goals
- No player scores twice in the same GW (dedup enforced at ingestion)
"""

import sqlite3
from init_db import DB_PATH


def get_team_goals_conceded(conn, match_id, club):
    """
    Goals conceded by club = opponent goals scored + own team's own goals.
    """
    c = conn.cursor()
    row = c.execute(
        "SELECT SUM(goals) FROM raw_stats WHERE match_id = ? AND club != ?",
        (match_id, club)
    ).fetchone()
    opponent_goals = row[0] or 0

    row = c.execute(
        "SELECT SUM(own_goals) FROM raw_stats WHERE match_id = ? AND club = ?",
        (match_id, club)
    ).fetchone()
    own_goals = row[0] or 0

    return int(opponent_goals) + int(own_goals)


def get_scoring_config(conn, season="2025-26"):
    """Load scoring config into a dict keyed by stat."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT stat, points, positions FROM scoring_config WHERE season = ?",
        (season,)
    ).fetchall()
    return {stat: (points, positions.split(",")) for stat, points, positions in rows}


def calc_minutes_points(minutes_played):
    """1pt for any play time 1-60, 2pt for 61+."""
    if minutes_played is None or minutes_played == 0:
        return 0
    if minutes_played >= 61:
        return 2
    return 1


def calc_player_score(conn, player_name, match_id, position, season="2025-26"):
    """
    Calculate fantasy score for a single player in a single match.
    Returns (score, breakdown_dict).
    """
    c = conn.cursor()
    config = get_scoring_config(conn, season)

    row = c.execute("""
        SELECT goals, assists, pk_saves, yellow_cards, red_cards,
               glc, lmt, elg, own_goals, motm,
               sub_off_min, sub_on_min, shots_on_target, key_passes,
               dribbles, tackles, interceptions, clearances,
               blocked_shots, saves, acc_crosses, acc_long_balls,
               minutes_played, club
        FROM raw_stats
        WHERE player_name = ? AND match_id = ?
    """, (player_name, match_id)).fetchone()

    if not row:
        return 0, {}

    (goals, assists, pk_saves, yellow_cards, red_cards,
     glc, lmt, elg, own_goals, motm,
     sub_off_min, sub_on_min, shots_on_target, key_passes,
     dribbles, tackles, interceptions, clearances,
     blocked_shots, saves, acc_crosses, acc_long_balls,
     minutes_played, club) = row

    pos = position.upper()
    breakdown = {}
    score = 0.0

    def add(stat_key, value, label=None):
        nonlocal score
        if stat_key not in config:
            return
        pts_per, eligible_positions = config[stat_key]
        if pos not in eligible_positions:
            return
        pts = value * pts_per
        if pts != 0:
            breakdown[label or stat_key] = pts
            score += pts

    # Standard stats
    add("goals",            goals)
    add("assists",          assists)
    add("pk_saves",         pk_saves)
    add("yellow_cards",     yellow_cards)
    add("red_cards",        red_cards)
    add("glc",              glc)
    add("lmt",              lmt)
    add("elg",              elg)
    add("own_goals",        own_goals)
    add("motm",             motm)
    add("shots_on_target",  shots_on_target)
    add("key_passes",       key_passes)
    add("dribbles",         dribbles)
    add("tackles",          tackles)
    add("interceptions",    interceptions)
    add("clearances",       clearances)
    add("blocked_shots",    blocked_shots)
    add("saves",            saves)
    add("acc_crosses",      acc_crosses)
    add("acc_long_balls",   acc_long_balls)

    # Minutes
    min_pts = calc_minutes_points(minutes_played)
    if min_pts > 0:
        breakdown["minutes"] = min_pts
        score += min_pts

    # Goals conceded - DEF
    if pos == "DEF":
        gc = get_team_goals_conceded(conn, match_id, club)
        gc_pts = gc * config["goals_conceded"][0]
        if gc_pts != 0:
            breakdown["goals_conceded"] = gc_pts
            score += gc_pts

    # Goals conceded - GK
    if pos == "GK":
        gc = get_team_goals_conceded(conn, match_id, club)
        gc_pts = gc * config["gk_goals_conceded"][0]
        if gc_pts != 0:
            breakdown["gk_goals_conceded"] = gc_pts
            score += gc_pts

    # Clean sheet - DEF or GK, 60+ minutes only
    if pos in ("DEF", "GK") and minutes_played is not None and minutes_played >= 60:
        gc = get_team_goals_conceded(conn, match_id, club)
        if gc == 0:
            cs_pts = config["clean_sheet"][0]
            breakdown["clean_sheet"] = cs_pts
            score += cs_pts

    return round(score, 2), breakdown


def calc_bulk_season_totals(conn, season, match_id_filter=None):
    """
    Total fantasy points per player for every match in `season`, computed in
    one pass instead of one calc_player_score() call per (player, match) —
    the per-call version re-runs several small queries each time, which is
    too slow across hundreds of players x dozens of matches.

    match_id_filter: optional (min, max) tuple to scope which raw_stats rows
    count as "this season" (mirrors the app's SEASON_CUTOFF heuristic, since
    raw_stats has no season column of its own).

    Returns dict: player_name -> {'total': rounded total fantasy points,
    'games': appearances, 'avg': rounded points per appearance}.
    """
    c = conn.cursor()
    config = get_scoring_config(conn, season)

    where = ""
    params = []
    if match_id_filter:
        lo, hi = match_id_filter
        where = " WHERE match_id >= ? AND match_id < ?"
        params = [lo, hi]

    rows = c.execute(f"""
        SELECT player_name, match_id, club, goals, assists, pk_saves,
               yellow_cards, red_cards, glc, lmt, elg, own_goals, motm,
               shots_on_target, key_passes, dribbles, tackles, interceptions,
               clearances, blocked_shots, saves, acc_crosses, acc_long_balls,
               minutes_played
        FROM raw_stats{where}
    """, params).fetchall()

    if not rows:
        return {}

    # Precompute goals conceded per (match_id, club) once instead of per-row.
    conceded = {}
    for match_id, club in {(r['match_id'], r['club']) for r in rows}:
        conceded[(match_id, club)] = get_team_goals_conceded(conn, match_id, club)

    positions = {r['name']: r['position'] for r in c.execute(
        "SELECT name, position FROM players"
    ).fetchall()}

    totals = {}
    games = {}
    for r in rows:
        name = r['player_name']
        pos = (positions.get(name) or 'MID').upper()
        score = 0.0

        def stat_pts(stat_key, value):
            if stat_key not in config or not value:
                return 0.0
            pts_per, eligible = config[stat_key]
            return value * pts_per if pos in eligible else 0.0

        score += stat_pts("goals", r['goals'])
        score += stat_pts("assists", r['assists'])
        score += stat_pts("pk_saves", r['pk_saves'])
        score += stat_pts("yellow_cards", r['yellow_cards'])
        score += stat_pts("red_cards", r['red_cards'])
        score += stat_pts("glc", r['glc'])
        score += stat_pts("lmt", r['lmt'])
        score += stat_pts("elg", r['elg'])
        score += stat_pts("own_goals", r['own_goals'])
        score += stat_pts("motm", r['motm'])
        score += stat_pts("shots_on_target", r['shots_on_target'])
        score += stat_pts("key_passes", r['key_passes'])
        score += stat_pts("dribbles", r['dribbles'])
        score += stat_pts("tackles", r['tackles'])
        score += stat_pts("interceptions", r['interceptions'])
        score += stat_pts("clearances", r['clearances'])
        score += stat_pts("blocked_shots", r['blocked_shots'])
        score += stat_pts("saves", r['saves'])
        score += stat_pts("acc_crosses", r['acc_crosses'])
        score += stat_pts("acc_long_balls", r['acc_long_balls'])

        score += calc_minutes_points(r['minutes_played'])

        gc = conceded.get((r['match_id'], r['club']), 0)
        if pos == "DEF":
            score += gc * config["goals_conceded"][0]
        if pos == "GK":
            score += gc * config["gk_goals_conceded"][0]
        if pos in ("DEF", "GK") and (r['minutes_played'] or 0) >= 60 and gc == 0:
            score += config["clean_sheet"][0]

        totals[name] = totals.get(name, 0.0) + score
        games[name] = games.get(name, 0) + 1

    return {
        name: {
            'total': round(total, 2),
            'games': games[name],
            'avg': round(total / games[name], 2) if games[name] else 0.0,
        }
        for name, total in totals.items()
    }


def calc_team_score_for_gw(conn, manager_id, gw_number, season="2025-26"):
    """
    Calculate total fantasy score for a manager in a given GW.
    Returns (total_score, player_breakdown_list).
    """
    c = conn.cursor()

    starters = c.execute("""
        SELECT player_name, position_slot
        FROM rosters
        WHERE manager_id = ?
          AND slot_type = 'starter'
          AND gw_start <= ?
          AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, gw_number, gw_number)).fetchall()

    if not starters:
        return 0, []

    match_ids = [row[0] for row in c.execute("""
        SELECT f.match_id FROM fixtures f
        JOIN gameweeks g ON f.gw_id = g.id
        WHERE g.gw_number = ? AND f.season = ?
    """, (gw_number, season)).fetchall()]

    total = 0.0
    breakdown = []

    for player_name, position_slot in starters:
        player_total = 0.0
        player_matches = []

        for match_id in match_ids:
            has_stats = c.execute(
                "SELECT 1 FROM raw_stats WHERE player_name = ? AND match_id = ?",
                (player_name, match_id)
            ).fetchone()

            if has_stats:
                score, detail = calc_player_score(conn, player_name, match_id, position_slot, season=season)
                player_total += score
                player_matches.append({
                    "match_id": match_id,
                    "score": score,
                    "detail": detail
                })

        breakdown.append({
            "player": player_name,
            "position": position_slot,
            "total": round(player_total, 2),
            "matches": player_matches
        })
        total += player_total

    return round(total, 2), breakdown


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    config = get_scoring_config(conn)
    print(f"Loaded {len(config)} scoring rules.")
    conn.close()
    print("Scoring engine ready.")
