"""
Server-side result generator for the "World Cup" draft-randomizer POC.

Pure function of its inputs plus `random` — no DB or Flask imports, so it's
independently testable. The whole result (goal tallies, tiebreakers, sudden
death, final draft order, and a full timestamped event stream for the
animation) is generated up front, before any animation plays.

Tie-shape-first strategy: rather than rolling 8 independent goal counts
(which can produce unpredictable, possibly-illegal tie patterns — 3+
isolated pairs, more than 2 groups needing a tiebreaker, etc.), we pick a
"tie shape" first (no ties / one cluster near the top / one near the bottom
/ one of each) and only then generate goal tallies consistent with that
shape. This guarantees "at most 2 tiebreaker matches" by construction
instead of hoping for it.

It's a scramble, not a shooting gallery: all 8 avatars contest a single
ball, and only whoever currently has it can shoot. The event stream mixes
"possession" events (the ball changes hands) with "shot" events (goal or
save), and every shot is immediately preceded by a possession event for
the same player.
"""

import random

# ── Tunables (adjust freely during POC playtesting) ─────────────────────────

NUM_PLAYERS = 8
SHAPE_WEIGHTS = {"none": 50, "top": 20, "bottom": 20, "both": 10}
CLUSTER_SIZE_RANGE = (2, 4)          # inclusive
TALLY_MIN, TALLY_MAX = 0, 8   # kept modest: each goal also costs ~2-3 possession-hop
                               # events, so total goals * (hops+1) * (1+save_rate) can
                               # balloon fast in a 45s window — tune down further if the
                               # animation still feels too frantic during playtesting

TIEBREAKER_TALLY_MAX = 4
TIEBREAKER_INTERNAL_TIE_CHANCE = 0.35   # real chance, not engineered away

MAIN_GAME_DURATION_MS = 45_000
TIEBREAKER_DURATION_MS = 18_000

MAIN_GAME_SAVE_RATE = 0.25           # extra non-scoring shots, as a fraction of goal count
TIEBREAKER_SAVE_RATE = 0.4
TIEBREAKER_MIN_EVENTS = 6            # floor so a low-scoring tiebreaker isn't empty

POSSESSION_HOPS_RANGE = (1, 2)       # inclusive, random hops between consecutive shots
POSSESSION_TO_SHOT_GAP_MS = (300, 600)
MIN_EVENT_SPACING_MS = 150
MAX_RESAMPLE_ATTEMPTS = 50


def generate_result(manager_ids, avatars):
    """
    manager_ids: list of manager ids (any hashable, we treat as opaque ids)
    avatars: dict {manager_id: emoji}
    Returns the full result dict — see module docstring / plan for shape.
    """
    manager_ids = list(manager_ids)
    if len(manager_ids) != NUM_PLAYERS:
        raise ValueError(f"World Cup sim requires exactly {NUM_PLAYERS} players, got {len(manager_ids)}")

    shape = _pick_shape()
    tally_by_rank = _generate_main_tallies(shape)

    shuffled = manager_ids[:]
    random.shuffle(shuffled)
    manager_by_rank = shuffled  # rank 0 = pick 1 = highest tally, assigned uniformly at random
    tally_by_manager = {manager_by_rank[rank]: tally for rank, tally in enumerate(tally_by_rank)}

    tied_clusters = _find_tied_clusters(manager_by_rank, tally_by_rank)
    assert len(tied_clusters) <= 2, "shape generation must never produce more than 2 tied clusters"

    main_players = [
        {"manager_id": mid, "emoji": avatars.get(mid), "final_tally": tally_by_manager[mid]}
        for mid in manager_ids
    ]
    main_events = _build_events(
        {mid: tally_by_manager[mid] for mid in manager_ids},
        duration_ms=MAIN_GAME_DURATION_MS,
        save_rate=MAIN_GAME_SAVE_RATE,
        min_events=0,
    )

    tiebreakers = []
    draft_order = list(manager_by_rank)  # start from main-game order, overwrite tied ranges below

    for cluster in tied_clusters:
        cluster_manager_ids = [manager_by_rank[r] for r in cluster["ranks"]]
        tb = _generate_tiebreaker(cluster_manager_ids)
        tiebreakers.append(tb)
        # Splice the tiebreaker's resolved order into the tied range of draft_order.
        start = cluster["ranks"][0]
        for offset, mid in enumerate(tb["resolved_order"]):
            draft_order[start + offset] = mid

    return {
        "main_game": {
            "duration_ms": MAIN_GAME_DURATION_MS,
            "players": main_players,
            "events": main_events,
        },
        "tiebreakers": [
            {
                "player_ids": tb["player_ids"],
                "duration_ms": tb["duration_ms"],
                "events": tb["events"],
                "final_tallies": {str(mid): t for mid, t in tb["final_tallies"].items()},
                "sudden_death": tb["sudden_death"],
            }
            for tb in tiebreakers
        ],
        "draft_order": draft_order,
    }


# ── Tie-shape + main tally generation ───────────────────────────────────────

def _weighted_choice(weights: dict):
    keys = list(weights.keys())
    weights_list = list(weights.values())
    return random.choices(keys, weights=weights_list, k=1)[0]


def _pick_shape():
    return _weighted_choice(SHAPE_WEIGHTS)


def _random_cluster_size():
    lo, hi = CLUSTER_SIZE_RANGE
    return random.randint(lo, hi)


def _generate_main_tallies(shape):
    """Returns a list of NUM_PLAYERS ints, index 0 = highest (pick 1), built
    from abstract rank slots only — no manager identity involved here."""
    if shape == "none":
        segments = [1] * NUM_PLAYERS  # each slot its own segment
    elif shape == "top":
        k = _random_cluster_size()
        segments = [k] + [1] * (NUM_PLAYERS - k)
    elif shape == "bottom":
        k = _random_cluster_size()
        segments = [1] * (NUM_PLAYERS - k) + [k]
    elif shape == "both":
        for _ in range(MAX_RESAMPLE_ATTEMPTS):
            k1 = _random_cluster_size()
            k2 = _random_cluster_size()
            if k1 + k2 <= NUM_PLAYERS:
                break
        else:
            k1, k2 = CLUSTER_SIZE_RANGE[0], CLUSTER_SIZE_RANGE[0]  # defensive fallback, always fits
        middle = NUM_PLAYERS - k1 - k2
        segments = [k1] + [1] * middle + [k2]
    else:
        raise ValueError(f"unknown shape {shape!r}")

    num_segments = len(segments)
    values = sorted(random.sample(range(TALLY_MIN, TALLY_MAX + 1), num_segments), reverse=True)

    tallies = []
    for seg_size, value in zip(segments, values):
        tallies.extend([value] * seg_size)
    assert len(tallies) == NUM_PLAYERS
    return tallies


def _find_tied_clusters(manager_by_rank, tally_by_rank):
    """Groups of consecutive ranks sharing the same tally (size >= 2)."""
    clusters = []
    i = 0
    while i < len(tally_by_rank):
        j = i + 1
        while j < len(tally_by_rank) and tally_by_rank[j] == tally_by_rank[i]:
            j += 1
        if j - i >= 2:
            clusters.append({"ranks": list(range(i, j)), "tally": tally_by_rank[i]})
        i = j
    return clusters


# ── Tiebreaker + sudden death ───────────────────────────────────────────────

def _generate_tiebreaker(cluster_manager_ids):
    n = len(cluster_manager_ids)
    internal_tie = random.random() < TIEBREAKER_INTERNAL_TIE_CHANCE

    sudden_death = None
    if not internal_tie:
        values = sorted(random.sample(range(0, TIEBREAKER_TALLY_MAX + 1), n), reverse=True)
        shuffled = cluster_manager_ids[:]
        random.shuffle(shuffled)  # unbiased: which manager gets which value is independent of input order
        order = shuffled
        tallies = {mid: v for mid, v in zip(shuffled, values)}
    else:
        w = random.randint(2, n)
        shuffled = cluster_manager_ids[:]
        random.shuffle(shuffled)
        tied_subset = shuffled[:w]
        rest = shuffled[w:]

        # top_value must leave room for len(rest) distinct values strictly
        # below it (i.e. drawn from range(0, top_value)), so it can't be
        # smaller than len(rest) itself.
        top_value = random.randint(max(1, len(rest)), TIEBREAKER_TALLY_MAX)
        rest_values = []
        if rest:
            rest_values = sorted(random.sample(range(0, top_value), len(rest)), reverse=True)

        tallies = {mid: top_value for mid in tied_subset}
        for mid, v in zip(rest, rest_values):
            tallies[mid] = v

        sudden_death_order = tied_subset[:]
        random.shuffle(sudden_death_order)
        sudden_death = {"players": sudden_death_order, "scorer": sudden_death_order[0]}

        order = sudden_death_order + [mid for mid, _ in sorted(
            zip(rest, rest_values), key=lambda p: -p[1]
        )]

    events = _build_events(
        tallies,
        duration_ms=TIEBREAKER_DURATION_MS,
        save_rate=TIEBREAKER_SAVE_RATE,
        min_events=TIEBREAKER_MIN_EVENTS,
    )

    return {
        "player_ids": cluster_manager_ids,
        "duration_ms": TIEBREAKER_DURATION_MS,
        "events": events,
        "final_tallies": tallies,
        "sudden_death": sudden_death,
        "resolved_order": order,
    }


# ── Event stream (possession + shot) generation ─────────────────────────────

def _build_events(tally_by_manager, duration_ms, save_rate, min_events):
    """Builds a chronologically-sorted stream of possession + shot events.

    Shots are generated first (one per goal, plus scattered saves), then for
    the gap before each shot we splice in 1-3 possession hops ending with the
    eventual shooter — this is what makes the ball visibly belong to whoever
    is about to shoot, and gives the scramble its constant back-and-forth.
    """
    manager_ids = list(tally_by_manager.keys())

    shot_specs = []  # (manager_id, outcome)
    for mid, goals in tally_by_manager.items():
        shot_specs.extend([(mid, "goal")] * goals)

    goal_count = len(shot_specs)
    save_count = max(0, round(goal_count * save_rate))
    for _ in range(save_count):
        shot_specs.append((random.choice(manager_ids), "save"))

    if len(shot_specs) < min_events:
        deficit = min_events - len(shot_specs)
        for _ in range(deficit):
            shot_specs.append((random.choice(manager_ids), "save"))

    random.shuffle(shot_specs)
    num_shots = len(shot_specs)

    if num_shots == 0:
        return []

    # Reserve headroom at the front of the window for possession-hop lead-in
    # before the very first shot. Walk forward with cumulative random gaps
    # that are NEVER smaller than MIN_SHOT_GAP_MS — this guarantees, by
    # construction, that every shot has enough room before it for its own
    # possession hops without ever bleeding into the previous shot's events
    # (a slot+jitter approach can't promise this: jitter near adjacent slot
    # boundaries can land shots closer together than a hop window needs).
    lead_in = POSSESSION_TO_SHOT_GAP_MS[1] * (POSSESSION_HOPS_RANGE[1] + 1)
    usable_start = lead_in
    usable_end = max(usable_start + 1, duration_ms - 200)
    window = usable_end - usable_start
    min_shot_gap = 2 * MIN_EVENT_SPACING_MS + 50

    if num_shots == 1:
        shot_times = [usable_start + window // 2]
    else:
        min_gap = min(min_shot_gap, max(1, window / num_shots) * 0.6)
        t = usable_start
        shot_times = []
        for i in range(num_shots):
            remaining_shots = num_shots - i
            remaining_window = max(1, usable_end - t)
            max_gap = max(min_gap + 1, remaining_window / remaining_shots * 1.6)
            t += random.uniform(min_gap, max_gap)
            shot_times.append(t)
        if shot_times[-1] > usable_end:
            scale = (usable_end - usable_start) / (shot_times[-1] - usable_start)
            shot_times = [usable_start + (t - usable_start) * scale for t in shot_times]
        shot_times = [round(t) for t in shot_times]

    events = []
    prev_holder = None
    prev_shot_time = 0  # hops for a shot must never land before the previous shot's own event,
                         # otherwise a global sort-by-t_ms can interleave two shots' hop sequences
                         # and put the wrong player's possession event immediately before a shot
    for (mid, outcome), t_shot in zip(shot_specs, shot_times):
        num_hops = random.randint(*POSSESSION_HOPS_RANGE)
        gap_before_shot = random.randint(*POSSESSION_TO_SHOT_GAP_MS)

        # Final hop is always the shooter gaining possession; earlier hops
        # (if any) are random other players contesting the ball beforehand.
        hop_holders = []
        for _ in range(num_hops - 1):
            candidates = [m for m in manager_ids if m != prev_holder] or manager_ids
            candidate = random.choice(candidates)
            hop_holders.append(candidate)
            prev_holder = candidate
        hop_holders.append(mid)
        prev_holder = mid

        window_floor = prev_shot_time + MIN_EVENT_SPACING_MS
        window_ceiling = t_shot - MIN_EVENT_SPACING_MS
        last_hop_time = min(window_ceiling, max(window_floor, t_shot - gap_before_shot))

        if num_hops == 1 or last_hop_time <= window_floor:
            hop_times = [last_hop_time]
            hop_holders = hop_holders[-1:]  # no room for lead-up hops — just the shooter's own
        else:
            window_start = max(window_floor, last_hop_time - (num_hops - 1) * MIN_EVENT_SPACING_MS)
            step = (last_hop_time - window_start) / (num_hops - 1)
            hop_times = [round(window_start + i * step) for i in range(num_hops)]

        for holder, t_hop in zip(hop_holders, hop_times):
            events.append({"t_ms": t_hop, "type": "possession", "manager_id": holder})

        events.append({"t_ms": t_shot, "type": "shot", "manager_id": mid, "outcome": outcome})
        prev_shot_time = t_shot

    events.sort(key=lambda e: e["t_ms"])
    return events


if __name__ == "__main__":
    import json
    result = generate_result(list(range(1, 9)), {i: "⚽" for i in range(1, 9)})
    print(json.dumps(result, indent=2)[:2000])
    print("...")
    print("draft_order:", result["draft_order"])
    print("tiebreakers:", len(result["tiebreakers"]))
