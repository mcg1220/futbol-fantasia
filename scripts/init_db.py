"""
Fútbol de Fantasía - Database initialization and seeding
Creates the SQLite schema and seeds it from the Google Sheets export.
"""

import sqlite3
import pandas as pd
from datetime import datetime, timedelta

DB_PATH = "../data/fantasia.db"
XLSX_PATH = "/Users/michaelgarcia/futbol_fantasia/data/Fútbol de Fantasia '25 - '26.xlsx"

def get_conn():
    return sqlite3.connect(DB_PATH)

def create_schema(conn):
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS scoring_config (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        stat TEXT NOT NULL,
        points REAL NOT NULL,
        positions TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS managers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        team_name TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        club TEXT NOT NULL,
        position TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS player_eligibility (
        player_id INTEGER NOT NULL,
        position TEXT NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (player_id, position),
        FOREIGN KEY (player_id) REFERENCES players(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS gameweeks (
        id INTEGER PRIMARY KEY,
        gw_number INTEGER NOT NULL UNIQUE,
        season TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'complete',
        is_playoff INTEGER NOT NULL DEFAULT 0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS fixtures (
        id INTEGER PRIMARY KEY,
        gw_id INTEGER NOT NULL,
        match_id INTEGER NOT NULL UNIQUE,
        home_club TEXT NOT NULL,
        away_club TEXT NOT NULL,
        match_date TEXT,
        kickoff_time TEXT,
        goals_home INTEGER,
        goals_away INTEGER,
        FOREIGN KEY (gw_id) REFERENCES gameweeks(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS raw_stats (
        id INTEGER PRIMARY KEY,
        match_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        club TEXT NOT NULL,
        gw_number INTEGER NOT NULL,
        goals INTEGER DEFAULT 0,
        assists INTEGER DEFAULT 0,
        pk_saves INTEGER DEFAULT 0,
        yellow_cards INTEGER DEFAULT 0,
        red_cards INTEGER DEFAULT 0,
        glc INTEGER DEFAULT 0,
        lmt INTEGER DEFAULT 0,
        elg INTEGER DEFAULT 0,
        own_goals INTEGER DEFAULT 0,
        motm INTEGER DEFAULT 0,
        sub_off_min INTEGER DEFAULT 0,
        sub_on_min INTEGER DEFAULT 0,
        shots_on_target INTEGER DEFAULT 0,
        key_passes INTEGER DEFAULT 0,
        dribbles INTEGER DEFAULT 0,
        tackles INTEGER DEFAULT 0,
        interceptions INTEGER DEFAULT 0,
        clearances INTEGER DEFAULT 0,
        blocked_shots INTEGER DEFAULT 0,
        saves INTEGER DEFAULT 0,
        acc_crosses INTEGER DEFAULT 0,
        acc_long_balls INTEGER DEFAULT 0,
        minutes_played INTEGER,
        FOREIGN KEY (match_id) REFERENCES fixtures(match_id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS rosters (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        slot_type TEXT NOT NULL,
        position_slot TEXT,
        gw_start INTEGER NOT NULL,
        gw_end INTEGER,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS matchups (
        id INTEGER PRIMARY KEY,
        gw_id INTEGER NOT NULL,
        manager1_id INTEGER NOT NULL,
        manager2_id INTEGER NOT NULL,
        is_playoff INTEGER NOT NULL DEFAULT 0,
        playoff_round TEXT,
        FOREIGN KEY (gw_id) REFERENCES gameweeks(id),
        FOREIGN KEY (manager1_id) REFERENCES managers(id),
        FOREIGN KEY (manager2_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY,
        gw_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        matchup_id INTEGER NOT NULL,
        fantasy_score REAL,
        win INTEGER,
        loss INTEGER,
        tie INTEGER,
        FOREIGN KEY (gw_id) REFERENCES gameweeks(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id),
        FOREIGN KEY (matchup_id) REFERENCES matchups(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        added_player TEXT,
        dropped_player TEXT,
        source TEXT NOT NULL,
        gw INTEGER,
        season TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS scraper_runs (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        gw_start INTEGER NOT NULL,
        gw_end INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL,
        total_fixtures INTEGER NOT NULL DEFAULT 0,
        perfect_count INTEGER NOT NULL DEFAULT 0,
        discrepancy_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        trigger TEXT NOT NULL DEFAULT 'manual'
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_order (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        manager_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        UNIQUE(season, manager_id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_windows (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        window_number INTEGER NOT NULL,
        gw INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        UNIQUE(season, window_number)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS waiver_claims (
        id INTEGER PRIMARY KEY,
        window_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        add_player TEXT NOT NULL,
        drop_player TEXT NOT NULL,
        priority INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        fail_reason TEXT,
        sequence_number INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(window_id, manager_id, priority),
        FOREIGN KEY (window_id) REFERENCES waiver_windows(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_pool (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        player_name TEXT NOT NULL,
        previous_club TEXT,
        added_by TEXT,
        added_at TEXT NOT NULL,
        UNIQUE(season, draft_type, player_name)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_drafts (
        id INTEGER PRIMARY KEY,
        season TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'not_started',
        round INTEGER NOT NULL DEFAULT 1,
        current_pick_number INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        UNIQUE(season, draft_type)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_draft_order (
        id INTEGER PRIMARY KEY,
        transfer_draft_id INTEGER NOT NULL,
        round INTEGER NOT NULL,
        position INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        UNIQUE(transfer_draft_id, round, position),
        FOREIGN KEY (transfer_draft_id) REFERENCES transfer_drafts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_draft_picks (
        id INTEGER PRIMARY KEY,
        transfer_draft_id INTEGER NOT NULL,
        round INTEGER NOT NULL,
        overall_pick INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        player_name TEXT,
        dropped_player TEXT,
        is_pass INTEGER NOT NULL DEFAULT 0,
        picked_at TEXT NOT NULL,
        FOREIGN KEY (transfer_draft_id) REFERENCES transfer_drafts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transfer_journalists (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        x_handle TEXT NOT NULL,
        notes TEXT,
        added_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        proposed_by TEXT,
        review_note TEXT,
        reviewed_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS meme_posts (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        post_type TEXT NOT NULL,
        image_path TEXT,
        link_url TEXT,
        link_type TEXT,
        embed_html TEXT,
        caption TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS meme_reactions (
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(post_id, manager_id, emoji),
        FOREIGN KEY (post_id) REFERENCES meme_posts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS meme_comments (
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (post_id) REFERENCES meme_posts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER,
        actor_name TEXT,
        entity_type TEXT NOT NULL,
        action TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail_json TEXT,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()
    print("Schema created.")

def seed_scoring_config(conn):
    c = conn.cursor()
    c.execute("DELETE FROM scoring_config WHERE season = '2025-26'")

    config = [
        ("goals",             7.0,   "GK,DEF,MID,FW"),
        ("assists",           5.0,   "GK,DEF,MID,FW"),
        ("shots_on_target",   0.5,   "GK,DEF,MID,FW"),
        ("key_passes",        0.5,   "GK,DEF,MID,FW"),
        ("dribbles",          0.5,   "GK,DEF,MID,FW"),
        ("tackles",           0.75,  "GK,DEF,MID,FW"),
        ("interceptions",     0.75,  "GK,DEF,MID,FW"),
        ("clearances",        0.25,  "GK,DEF,MID,FW"),
        ("acc_crosses",       0.5,   "GK,DEF,MID,FW"),
        ("own_goals",        -4.0,   "GK,DEF,MID,FW"),
        ("yellow_cards",     -1.0,   "GK,DEF,MID,FW"),
        ("red_cards",        -2.0,   "GK,DEF,MID,FW"),
        ("lmt",               1.0,   "GK,DEF,MID,FW"),
        ("elg",              -1.0,   "GK,DEF,MID,FW"),
        ("motm",              2.0,   "GK,DEF,MID,FW"),
        ("glc",               1.75,  "GK,DEF,MID,FW"),
        ("blocked_shots",     1.5,   "DEF,MID,FW"),
        ("acc_long_balls",    0.25,  "DEF,MID,FW"),
        ("goals_conceded",   -0.5,   "DEF"),
        ("clean_sheet",       4.0,   "DEF,GK"),
        ("saves",             1.75,  "GK"),
        ("pk_saves",          1.75,  "GK"),
        ("gk_goals_conceded",-1.0,   "GK"),
        ("minutes_1_to_60",   1.0,   "GK,DEF,MID,FW"),
        ("minutes_61_plus",   1.0,   "GK,DEF,MID,FW"),
    ]

    for stat, points, positions in config:
        c.execute(
            "INSERT INTO scoring_config (season, stat, points, positions) VALUES (?, ?, ?, ?)",
            ("2025-26", stat, points, positions)
        )

    conn.commit()
    print(f"Seeded {len(config)} scoring config entries.")

def seed_managers(conn):
    c = conn.cursor()
    c.execute("DELETE FROM managers")
    managers = [
        (1, "Mike",  "Kame-Kame-HAAA(land)"),
        (2, "Remy",  "Dec the Hall"),
    ]
    c.executemany("INSERT OR REPLACE INTO managers (id, name, team_name) VALUES (?, ?, ?)", managers)
    conn.commit()
    print(f"Seeded {len(managers)} managers.")

def excel_date_to_iso(excel_date):
    try:
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=int(excel_date))).strftime("%Y-%m-%d")
    except:
        return None

def seed_raw_stats(conn):
    df = pd.read_excel(XLSX_PATH, sheet_name="25-26 Stats")
    df.columns = [c.strip() for c in df.columns]

    rename_map = {
        "matchid": "match_id", "player": "player_name", "date": "match_date_raw",
        "team": "club", "opponent": "opponent_raw", "G": "goals", "A": "assists",
        "PKSave": "pk_saves", "Ylw": "yellow_cards", "Red": "red_cards",
        "GLC": "glc", "LMT": "lmt", "ELG": "elg", "OG": "own_goals", "MotM": "motm",
        "SubOffMin": "sub_off_min", "SubOnMin": "sub_on_min", "SoT": "shots_on_target",
        "KP": "key_passes", "Drb": "dribbles", "tkl": "tackles", "int": "interceptions",
        "clr": "clearances", "blk": "blocked_shots", "saves": "saves",
        "AccCross": "acc_crosses", "AccLB": "acc_long_balls", "GW": "gw_number",
        "Loc": "loc",
    }
    df = df.rename(columns=rename_map)
    df = df[df["match_id"].notna()].copy()
    df["match_id"] = df["match_id"].astype(int)
    df["gw_number"] = df["gw_number"].astype(int)
    df["match_date"] = df["match_date_raw"].apply(excel_date_to_iso)

    numeric_cols = [
        "goals", "assists", "pk_saves", "yellow_cards", "red_cards",
        "glc", "lmt", "elg", "own_goals", "motm", "shots_on_target",
        "key_passes", "dribbles", "tackles", "interceptions", "clearances",
        "blocked_shots", "saves", "acc_crosses", "acc_long_balls",
        "sub_on_min", "sub_off_min"
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    def calc_minutes(row):
        sub_on  = row["sub_on_min"]   # 0 = started; >0 = came on at this minute
        sub_off = row["sub_off_min"]  # 0 = not subbed off; >0 = subbed off at this minute
        if sub_on == 0 and sub_off == 0:
            return 90
        elif sub_on == 0 and sub_off > 0:
            return int(sub_off)
        elif sub_on > 0 and sub_off == 0:
            return 90 - int(sub_on)
        else:
            return max(0, int(sub_off) - int(sub_on))

    df["minutes_played"] = df.apply(calc_minutes, axis=1)

    # Seed gameweeks
    c = conn.cursor()
    c.execute("DELETE FROM gameweeks")
    gws = sorted(df["gw_number"].unique())
    playoff_gws = {34, 35, 36, 37}
    for gw in gws:
        is_playoff = 1 if gw in playoff_gws else 0
        status = "complete" if gw <= 33 else "incomplete"
        c.execute(
            "INSERT OR REPLACE INTO gameweeks (gw_number, season, status, is_playoff) VALUES (?, '2025-26', ?, ?)",
            (int(gw), status, is_playoff)
        )
    conn.commit()
    print(f"Seeded {len(gws)} gameweeks (GW {min(gws)}-{max(gws)}).")

    # Seed fixtures
    c.execute("DELETE FROM fixtures")
    fixtures_seen = {}
    for _, row in df.iterrows():
        mid = row["match_id"]
        if mid not in fixtures_seen:
            gw_id = c.execute(
                "SELECT id FROM gameweeks WHERE gw_number = ?", (row["gw_number"],)
            ).fetchone()[0]
            loc = str(row.get("loc", "")).strip()
            if loc == "Home":
                home_club, away_club = row["club"], str(row.get("opponent_raw", "")).strip()
            else:
                away_club, home_club = row["club"], str(row.get("opponent_raw", "")).strip()
            c.execute(
                "INSERT OR IGNORE INTO fixtures (gw_id, match_id, home_club, away_club, match_date) VALUES (?, ?, ?, ?, ?)",
                (gw_id, mid, home_club, away_club, row["match_date"])
            )
            fixtures_seen[mid] = True
    conn.commit()
    print(f"Seeded {len(fixtures_seen)} fixtures.")

    # Derive match scores from goals + own goals
    rows = c.execute("SELECT match_id, club, SUM(goals) as g, SUM(own_goals) as og FROM raw_stats GROUP BY match_id, club").fetchall()
    from collections import defaultdict
    match_goals = defaultdict(dict)
    for match_id, club, g, og in rows:
        match_goals[match_id][club] = {"goals": g or 0, "og": og or 0}

    # Seed raw_stats
    c.execute("DELETE FROM raw_stats")
    insert_cols = [
        "match_id", "player_name", "club", "gw_number", "goals", "assists", "pk_saves",
        "yellow_cards", "red_cards", "glc", "lmt", "elg", "own_goals", "motm",
        "sub_on_min", "sub_off_min", "shots_on_target", "key_passes", "dribbles",
        "tackles", "interceptions", "clearances", "blocked_shots", "saves",
        "acc_crosses", "acc_long_balls", "minutes_played"
    ]
    stat_rows = df[insert_cols].values.tolist()
    c.executemany(
        f"INSERT INTO raw_stats ({', '.join(insert_cols)}) VALUES ({', '.join(['?']*len(insert_cols))})",
        stat_rows
    )
    conn.commit()
    print(f"Seeded {len(stat_rows)} raw stat rows.")

    # Update fixture scores using opponent goals + own goals
    for match_id, clubs in match_goals.items():
        fixture = c.execute(
            "SELECT home_club, away_club FROM fixtures WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not fixture:
            continue
        home_club, away_club = fixture
        home_data = clubs.get(home_club, {"goals": 0, "og": 0})
        away_data = clubs.get(away_club, {"goals": 0, "og": 0})
        # Goals conceded = opponent goals + own own_goals
        goals_home = away_data["goals"] + home_data["og"]
        goals_away = home_data["goals"] + away_data["og"]
        c.execute(
            "UPDATE fixtures SET goals_home = ?, goals_away = ? WHERE match_id = ?",
            (goals_home, goals_away, match_id)
        )
    conn.commit()
    print("Updated fixture scores.")

def seed_gameweek_matchups(conn):
    c = conn.cursor()
    c.execute("DELETE FROM matchups")
    mike_id = c.execute("SELECT id FROM managers WHERE name='Mike'").fetchone()[0]
    remy_id = c.execute("SELECT id FROM managers WHERE name='Remy'").fetchone()[0]
    for gw in [5, 12, 19, 26, 33]:
        gw_row = c.execute("SELECT id FROM gameweeks WHERE gw_number=?", (gw,)).fetchone()
        if gw_row:
            c.execute(
                "INSERT INTO matchups (gw_id, manager1_id, manager2_id, is_playoff) VALUES (?, ?, ?, 0)",
                (gw_row[0], mike_id, remy_id)
            )
    conn.commit()
    print("Seeded matchups.")

if __name__ == "__main__":
    conn = get_conn()
    create_schema(conn)
    seed_scoring_config(conn)
    seed_managers(conn)
    seed_raw_stats(conn)
    seed_gameweek_matchups(conn)
    conn.close()
    print("\nDatabase initialized successfully.")
