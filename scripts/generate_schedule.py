"""
Fútbol de Fantasía - Schedule Generator
Generates a 33-gameweek head-to-head schedule for 8 fantasy teams.

Approach:
1. Run a double round-robin TWICE (28 GWs total) - each team plays every
   other team 4 times (2x home, 2x away)
2. 5 extra GWs - balanced random extra matchups, minimizing repeat pairings

Usage:
    python3 generate_schedule.py --season 2026-27

RUN THIS SCRIPT IN TERMINAL

"""

import sqlite3
import argparse
import random
from collections import defaultdict
from init_db import DB_PATH

TEAM_NAMES = ["Mike", "Tom", "Remy", "Fish", "Armand", "Jack", "Nick", "John"]


def round_robin_rounds(teams):
    """
    Standard circle method for round-robin scheduling.
    Returns a list of rounds, each round a list of (team_a, team_b) pairs.
    For n teams, produces n-1 rounds covering every pair once.
    """
    n = len(teams)
    if n % 2 != 0:
        teams = teams + [None]  # bye
        n += 1

    rounds = []
    arr = teams[:]
    for _ in range(n - 1):
        round_pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            if a is not None and b is not None:
                round_pairs.append((a, b))
        rounds.append(round_pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return rounds


def double_round_robin(teams):
    """Returns 2*(n-1) rounds, second leg mirrors first with home/away swapped."""
    first_leg = round_robin_rounds(teams)
    second_leg = [[(b, a) for (a, b) in round] for round in first_leg]
    return first_leg + second_leg


def generate_extra_gameweeks(teams, num_extra_gws, pair_counts):
    """
    Generate num_extra_gws additional rounds of pairings, balancing how many
    times each pair has already played. Each round pairs every team once.
    """
    extra_rounds = []
    for _ in range(num_extra_gws):
        best_pairs, best_cost = None, None
        for attempt in range(300):
            shuffled = teams[:]
            random.shuffle(shuffled)
            pairs = [(shuffled[i], shuffled[i + 1]) for i in range(0, len(shuffled), 2)]
            cost = sum(pair_counts[frozenset(p)] for p in pairs)
            if best_cost is None or cost < best_cost:
                best_pairs, best_cost = pairs, cost

        round_pairs = []
        for a, b in best_pairs:
            if random.random() < 0.5:
                a, b = b, a
            round_pairs.append((a, b))
            pair_counts[frozenset((a, b))] += 1

        extra_rounds.append(round_pairs)

    return extra_rounds


def build_schedule(team_ids, num_regular_gws=33):
    """
    Returns dict: gw_number -> list of (team_a_id, team_b_id) tuples.
    28 GWs = double round-robin x2 (4 meetings per pair).
    5 GWs = balanced extras.
    """
    drr_x2 = double_round_robin(team_ids) + double_round_robin(team_ids)  # 28 rounds

    pair_counts = defaultdict(int)
    for round_pairs in drr_x2:
        for a, b in round_pairs:
            pair_counts[frozenset((a, b))] += 1

    num_extra = num_regular_gws - len(drr_x2)  # 33 - 28 = 5
    extra = generate_extra_gameweeks(team_ids, num_extra, pair_counts) if num_extra > 0 else []

    all_rounds = drr_x2 + extra
    random.shuffle(all_rounds)

    schedule = {}
    for gw_num, round_pairs in enumerate(all_rounds[:num_regular_gws], start=1):
        schedule[gw_num] = round_pairs

    return schedule


def save_schedule(schedule, season):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS matchups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season TEXT,
            gw_number INTEGER,
            team_a_id INTEGER,
            team_b_id INTEGER
        )
    """)

    for gw_num, pairs in schedule.items():
        for team_a, team_b in pairs:
            c.execute(
                "INSERT INTO matchups (season, gw_number, team_a_id, team_b_id) VALUES (?,?,?,?)",
                (season, gw_num, team_a, team_b)
            )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=str, default="2026-27")
    parser.add_argument("--num_gws", type=int, default=33)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    team_ids = [r[0] for r in c.execute("SELECT id FROM managers").fetchall()]
    id_to_name = {r[0]: r[1] for r in c.execute("SELECT id, name FROM managers").fetchall()}
    conn.close()

    if len(team_ids) != 8:
        print(f"Warning: expected 8 teams, found {len(team_ids)}. Using placeholder names: {TEAM_NAMES}")

    schedule = build_schedule(team_ids, num_regular_gws=args.num_gws)

    for gw_num in sorted(schedule.keys()):
        pairs = schedule[gw_num]
        matchup_strs = [f"{id_to_name.get(a, a)} vs {id_to_name.get(b, b)}" for a, b in pairs]
        print(f"GW{gw_num}: " + ", ".join(matchup_strs))

    save_schedule(schedule, args.season)
    print(f"\nSaved {args.num_gws} gameweeks of matchups for {args.season} to 'matchups' table.")