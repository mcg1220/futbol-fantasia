"""
Read-only analysis: what would changing the dribbles scoring value from
0.5 to 0.75 pts do to 2025-26 season fantasy totals?

Dribbles are a flat linear stat (no thresholds/caps) eligible for every
position, so the impact is exact and simple: each player's point total goes
up by 0.25 x their season dribble count. This script computes real current
totals via the actual scoring engine, adds that exact delta, and reports
per-player and per-position impact plus rank shifts.

Does NOT write to the database or touch scoring_config in any way.

Run: python3 analyze_dribble_scoring_change.py > /tmp/dribble_analysis.json
"""

import json
import sqlite3
from init_db import DB_PATH
from scoring_engine import calc_bulk_season_totals

SEASON = '2025-26'
SEASON_CUTOFF = 1983000  # raw_stats has no season column; WhoScored match_ids below this are 2025-26
DELTA_PER_DRIBBLE = 0.25  # 0.75 - 0.50


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Real current-rules totals, via the actual scoring engine (not a re-derivation).
    current = calc_bulk_season_totals(conn, SEASON, match_id_filter=(0, SEASON_CUTOFF))

    # Season dribble totals + games played (for dribbles/90) + position, per player.
    dribble_rows = conn.execute("""
        SELECT player_name, SUM(dribbles) AS dribbles, COUNT(*) AS games,
               SUM(minutes_played) AS minutes
        FROM raw_stats
        WHERE match_id < ?
        GROUP BY player_name
        HAVING SUM(dribbles) > 0
    """, (SEASON_CUTOFF,)).fetchall()

    positions = {r['name']: r['position'] for r in conn.execute("SELECT name, position FROM players").fetchall()}

    players = []
    total_dribbles = 0
    for row in dribble_rows:
        name = row['player_name']
        dribbles = row['dribbles'] or 0
        total_dribbles += dribbles

        cur = current.get(name, {'total': 0.0, 'avg': 0.0, 'games': row['games']})
        delta = round(dribbles * DELTA_PER_DRIBBLE, 2)
        new_total = round(cur['total'] + delta, 2)
        games = cur['games'] or row['games'] or 1
        new_avg = round(new_total / games, 2) if games else 0.0
        pct = round((delta / cur['total']) * 100, 1) if cur['total'] else None
        minutes = row['minutes'] or 0

        players.append({
            'name': name,
            'position': positions.get(name, '?'),
            'dribbles': dribbles,
            'dribbles_per_90': round(dribbles / (minutes / 90), 2) if minutes else 0.0,
            'games': games,
            'current_total': round(cur['total'], 2),
            'current_avg': cur['avg'],
            'new_total': new_total,
            'new_avg': new_avg,
            'delta_pts': delta,
            'delta_pct': pct,
        })

    # Rank shifts: rank by total pts, current rules vs new rules, across
    # every scored player (not just dribblers) so a dribbler's climb past
    # non-dribblers is visible too.
    all_current_ranked = sorted(current.items(), key=lambda kv: -kv[1]['total'])
    current_rank = {name: i + 1 for i, (name, _) in enumerate(all_current_ranked)}

    new_totals_all = {name: v['total'] for name, v in current.items()}
    for p in players:
        new_totals_all[p['name']] = p['new_total']
    all_new_ranked = sorted(new_totals_all.items(), key=lambda kv: -kv[1])
    new_rank = {name: i + 1 for i, (name, _) in enumerate(all_new_ranked)}

    for p in players:
        p['current_rank'] = current_rank.get(p['name'])
        p['new_rank'] = new_rank.get(p['name'])
        p['rank_change'] = (p['current_rank'] - p['new_rank']) if p['current_rank'] and p['new_rank'] else 0

    players.sort(key=lambda p: -p['delta_pts'])

    # Position-level aggregation.
    by_position = {}
    for p in players:
        pos = p['position'] or '?'
        agg = by_position.setdefault(pos, {'players': 0, 'total_delta': 0.0, 'total_dribbles': 0})
        agg['players'] += 1
        agg['total_delta'] += p['delta_pts']
        agg['total_dribbles'] += p['dribbles']
    for pos, agg in by_position.items():
        agg['total_delta'] = round(agg['total_delta'], 2)
        agg['avg_delta_per_player'] = round(agg['total_delta'] / agg['players'], 2) if agg['players'] else 0.0

    summary = {
        'season': SEASON,
        'delta_per_dribble': DELTA_PER_DRIBBLE,
        'players_with_dribbles': len(players),
        'total_dribbles_recorded': total_dribbles,
        'total_league_point_inflation': round(total_dribbles * DELTA_PER_DRIBBLE, 2),
        'top_10_by_delta': [p['name'] for p in players[:10]],
        'biggest_rank_climb': max(players, key=lambda p: p['rank_change'])['name'] if players else None,
    }

    conn.close()

    print(json.dumps({
        'summary': summary,
        'by_position': by_position,
        'players': players,
    }, indent=2))


if __name__ == '__main__':
    main()
