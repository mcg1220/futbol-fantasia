import os
import re
import sys
import sqlite3
import json
import threading
import subprocess
import signal
import time
import uuid
import random
import traceback
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB upload cap (memes: images/GIFs + flattened canvas exports)
# Falls back to a fixed local-dev value so `python app.py` still works with no
# env vars set; every hosted deploy must set a real SECRET_KEY explicitly.
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key-set-SECRET_KEY-in-production')

# Login sessions: a signed cookie (via secret_key above), not a server-side
# store — fine for 8 trusted friends where nothing sensitive is stored
# beyond an integer manager id, and it avoids a sessions table + cleanup
# job for a problem this app doesn't have. `permanent=True` (set at login,
# below) plus this lifetime is what satisfies "don't make me log in every
# time" — Flask refreshes the cookie's expiry on every response by default.
app.permanent_session_lifetime = timedelta(days=90)
# Secure cookies need HTTPS, which is only true once actually deployed —
# forcing it on for local `python app.py` over plain http would silently
# break the cookie. Same FLASK_DEBUG convention used at the bottom of this
# file to gate Flask's debug mode.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_DEBUG', 'true').lower() != 'true'


@app.template_filter('js_string')
def js_string_filter(value):
    """Escape a value for interpolation inside a single-quoted JS string
    literal embedded in an HTML attribute, e.g. onclick="fn('{{ x|js_string }}')".

    Player names like "Nico O'Reilly" broke every onclick handler that
    interpolated the raw name into a single-quoted JS string: the apostrophe
    terminated the string early, leaving invalid JS, so the click silently did
    nothing. Jinja's autoescaping doesn't prevent this — it HTML-escapes the
    apostrophe to `&#39;`, but the browser decodes that back to a literal `'`
    before handing the attribute value to the JS engine, so the string still
    breaks. Escaping backslash and apostrophe here (before Jinja's own
    autoescaping runs) survives that round trip: `'` becomes `\'`, Jinja turns
    that into `\&#39;`, the browser decodes it to `\'`, and JS reads that as an
    escaped quote rather than the end of the string.
    """
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


DB_PATH             = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
SCRAPER_STATUS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'scraper_status.json')
SCRIPTS_DIR         = os.path.join(os.path.dirname(__file__), 'scripts')
BADGES_PATH         = os.path.join(os.path.dirname(__file__), 'data', 'badges.json')
PHOTOS_DIR          = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'team_photos')
ALLOWED_PHOTO_EXTS  = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MEMES_DIR           = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'memes')
ALLOWED_MEME_EXTS   = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

sys.path.insert(0, SCRIPTS_DIR)
from scoring_engine import calc_player_score, get_scoring_config, calc_bulk_season_totals, get_team_goals_conceded, calc_team_score_for_gw
import world_cup_sim
import scraper as scraper_lib
# scraper.py's own save_to_db() assumes its module is run with cwd=scripts/
# (see init_db.DB_PATH, a relative path) -- redirect it to the same
# absolute DB_PATH this process already uses, so calling it directly from
# here (not as a scripts/ subprocess) still writes to the right file.
scraper_lib.DB_PATH = DB_PATH

SEASON_CUTOFF = 1983000  # raw_stats has no season column; WhoScored match_ids below this are 2025-26

# Relegated after 2025-26: historical stats stay, but these clubs' players are
# excluded from the current-season browse/draft pools (unless they transfer to
# a PL club, at which point their club field gets updated by a scrape).
RELEGATED_CLUBS = ('Wolves', 'West Ham', 'Burnley')

# Transfer Room: X (Twitter) List URL powering the embedded journalist feed.
# Empty until a List is created on X containing the tracked journalists —
# the page shows a placeholder until this is set. See working instructions/
# transfer_room_spec.pdf for setup steps.
TRANSFER_LIST_URL = "https://x.com/i/lists/2083020964476944700"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets reads and writes happen concurrently instead of blocking each
    # other (SQLite's default journal mode serializes them) — a real
    # reliability improvement once this is a hosted app with several
    # managers hitting it at once instead of just one person locally.
    # journal_mode is stored in the db file itself, so this is a one-time
    # switch in practice, but setting it on every connection is a cheap
    # no-op once it's already WAL and guarantees a freshly-created db (e.g.
    # a brand new deploy before data is migrated in) starts in WAL too.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ── Auth (session-based PIN login) ──────────────────────────────────────────
# Reads/GETs stay public (anyone can browse standings/rosters/draft board);
# only writes need a real, non-spoofable identity. See /login below for how
# session['manager_id'] gets set.

LOGIN_EXEMPT_PATHS_EXACT = {
    '/login', '/internal/auto-scrape-trigger',
    # World Cup draft-randomizer generator — pure RNG, no DB writes, no
    # manager identity needed to watch it run (see /draft-randomizer-poc
    # and the real /draft page). The endpoint that actually persists a
    # result, /api/world-cup-sim/lock-in, is deliberately NOT exempt here —
    # locking in the real draft order still requires login.
    '/api/world-cup-sim/generate',
}


@app.before_request
def require_login_for_writes():
    if (request.method == 'GET'
            or request.path in LOGIN_EXEMPT_PATHS_EXACT
            or request.path.startswith('/login')):
        return
    if 'manager_id' not in session:
        return jsonify({"error": "Not logged in"}), 401


def current_manager_id():
    return session.get('manager_id')


def get_my_shortlist(conn):
    """Player names the logged-in manager has starred — [] if not logged in.
    Used by the Main Draft pool and the Add/Drop watchlist, which both
    render the same underlying per-manager shortlist."""
    manager_id = current_manager_id()
    if not manager_id:
        return []
    rows = conn.execute("SELECT player_name FROM shortlists WHERE manager_id=?", (manager_id,)).fetchall()
    return [r['player_name'] for r in rows]


@app.context_processor
def inject_current_manager():
    manager_id = session.get('manager_id')
    if not manager_id:
        return {'current_manager_name': None, 'current_manager_id': None,
                'pending_trades_count': 0, 'newly_accepted_trades': []}
    conn = get_db()
    row = conn.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()

    pending_trades_count = conn.execute(
        "SELECT COUNT(*) FROM player_trades WHERE target_manager_id=? AND status='pending'", (manager_id,)
    ).fetchone()[0]

    # One-time "your trade was accepted" confirmation for the proposer — a
    # trade can be accepted by the other manager at any time, async from the
    # proposer's own session, so this is surfaced as a site-wide banner the
    # next time the proposer loads any page, then marked notified so it
    # doesn't show again.
    newly_accepted_rows = conn.execute("""
        SELECT t.id, tgt.name AS target_name
        FROM player_trades t
        JOIN managers tgt ON tgt.id = t.target_manager_id
        WHERE t.proposer_manager_id=? AND t.status='accepted' AND t.proposer_notified=0
    """, (manager_id,)).fetchall()

    newly_accepted_trades = []
    for t in newly_accepted_rows:
        items = conn.execute(
            "SELECT player_name, from_manager_id FROM player_trade_items WHERE trade_id=?", (t['id'],)
        ).fetchall()
        newly_accepted_trades.append({
            'target_name': t['target_name'],
            'give': [i['player_name'] for i in items if i['from_manager_id'] == manager_id],
            'receive': [i['player_name'] for i in items if i['from_manager_id'] != manager_id],
        })

    if newly_accepted_rows:
        conn.executemany(
            "UPDATE player_trades SET proposer_notified=1 WHERE id=?",
            [(t['id'],) for t in newly_accepted_rows]
        )
        conn.commit()

    conn.close()
    return {
        'current_manager_name': row['name'] if row else None,
        'current_manager_id': manager_id,
        'pending_trades_count': pending_trades_count,
        'newly_accepted_trades': newly_accepted_trades,
    }


def log_audit(conn, manager_id, entity_type, action, summary, detail=None):
    """
    Records one row in the unified audit_log — covers everything EXCEPT
    Locker Room, Transfer Room, and the Scraper, which already have their
    own dedicated logs. Does not commit; caller commits as part of its
    existing transaction. actor_name is captured now (not joined at read
    time) so history still reads correctly after a manager renames later.
    """
    actor_name = None
    if manager_id:
        row = conn.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()
        actor_name = row['name'] if row else None
    conn.execute("""
        INSERT INTO audit_log (manager_id, actor_name, entity_type, action, summary, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (manager_id, actor_name or 'Unknown', entity_type, action, summary,
          json.dumps(detail) if detail else None, now_eastern_naive().isoformat()))


def load_badges():
    try:
        with open(BADGES_PATH) as f:
            return json.load(f)
    except:
        return {}


def read_scraper_status():
    try:
        with open(SCRAPER_STATUS_PATH) as f:
            return json.load(f)
    except:
        return {"status": "idle", "started_at": None, "completed_at": None, "gw": None}


def write_scraper_status(data):
    with open(SCRAPER_STATUS_PATH, 'w') as f:
        json.dump(data, f)


def get_current_gw(conn, season):
    """The gw a manager should be managing right now: one past the last gw
    that's actually been closed (see finalize_gw_results), or gw 1 if none
    has closed yet. Deliberately NOT "the last gw with results" -- that
    would keep pointing at a gw that's already over (and permanently
    locked) for the entire multi-week gap until the *next* gw also closes,
    which silently blocked trades/swaps against it via is_gw_locked."""
    row = conn.execute("""
        SELECT MAX(g.gw_number)
        FROM results r
        JOIN gameweeks g ON g.id = r.gw_id
        WHERE g.season = ?
    """, (season,)).fetchone()[0]
    last_closed = row or 0
    return min(last_closed + 1, 33)


def gw_change_label(conn, season, gw):
    """'GW5 (current)' / 'GW6 (future)' / 'GW3 (past)' — for audit-log
    summaries, so it's clear at a glance whether a roster change was made
    live for the active gw or pre-set for a later one via the Plan Future
    Lineup panel."""
    current = get_current_gw(conn, season)
    if gw == current:
        tag = 'current'
    elif gw > current:
        tag = 'future'
    else:
        tag = 'past'
    return f"GW{gw} ({tag})"


def get_current_pl_clubs(conn):
    """Distinct clubs currently in the top flight — every real club a
    manually-typed club field should be allowed to match, excluding
    relegated clubs (still present historically but not currently active)."""
    rows = conn.execute(f"""
        SELECT DISTINCT club FROM players
        WHERE club IS NOT NULL AND club != ''
          AND club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
          AND draftable = 1
        ORDER BY club
    """, RELEGATED_CLUBS).fetchall()
    return [r['club'] for r in rows]


def get_owner_map(conn, season, gw):
    """player_name -> {'manager_id', 'manager_name', 'team_name'} for whoever owns them this GW."""
    rows = conn.execute("""
        SELECT r.player_name, m.id AS manager_id, m.name AS manager_name, m.team_name
        FROM rosters r
        JOIN managers m ON m.id = r.manager_id
        WHERE r.gw_start <= ? AND (r.gw_end IS NULL OR r.gw_end >= ?)
    """, (gw, gw)).fetchall()
    return {r['player_name']: dict(r) for r in rows}


def format_kickoff(match_date, kickoff_time):
    """'2026-08-21', '15:00' -> ('Fri 8/21', '3:00 PM ET'). Either input can be
    None/empty (fixture not yet scheduled), in which case both outputs are None."""
    if not match_date or not kickoff_time:
        return None, None
    date_obj = datetime.strptime(match_date, '%Y-%m-%d')
    time_obj = datetime.strptime(kickoff_time, '%H:%M')
    return date_obj.strftime('%a %-m/%-d'), time_obj.strftime('%-I:%M %p') + ' ET'


def get_gw_fixture_info(conn, season, gw_number):
    """club -> {'opponent', 'date', 'time', 'match_id'} for a single gameweek.
    `date`/`time` are the formatted strings from format_kickoff (None if not
    yet scheduled)."""
    rows = conn.execute("""
        SELECT f.match_id, f.home_club, f.away_club, f.match_date, f.kickoff_time
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ?
    """, (gw_number, season)).fetchall()

    info = {}
    for r in rows:
        date_label, time_label = format_kickoff(r['match_date'], r['kickoff_time'])
        for club, opponent in ((r['home_club'], r['away_club']), (r['away_club'], r['home_club'])):
            info[club] = {'opponent': opponent, 'date': date_label, 'time': time_label, 'match_id': r['match_id']}
    return info


def get_gw_kickoff_bounds(conn, season, gw_number):
    """(earliest, latest) kickoff datetimes (naive, Eastern wall-clock — same
    convention as the stored match_date/kickoff_time) across a gw's
    scheduled fixtures, or (None, None) if none are scheduled yet."""
    rows = conn.execute("""
        SELECT match_date, kickoff_time FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ?
          AND match_date IS NOT NULL AND kickoff_time IS NOT NULL
    """, (gw_number, season)).fetchall()
    if not rows:
        return None, None
    kickoffs = [datetime.strptime(f"{r['match_date']} {r['kickoff_time']}", '%Y-%m-%d %H:%M') for r in rows]
    return min(kickoffs), max(kickoffs)


def get_player_kickoff(conn, season, gw_number, club):
    """This club's kickoff datetime (naive Eastern) for the gw, or None if
    the club has no fixture this gw or it isn't scheduled yet."""
    if not club:
        return None
    row = conn.execute("""
        SELECT match_date, kickoff_time FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ? AND (f.home_club = ? OR f.away_club = ?)
    """, (gw_number, season, club, club)).fetchone()
    if not row or not row['match_date'] or not row['kickoff_time']:
        return None
    return datetime.strptime(f"{row['match_date']} {row['kickoff_time']}", '%Y-%m-%d %H:%M')


def now_eastern_naive():
    """Current time as a naive datetime in America/New_York — comparable
    directly against the naive Eastern wall-clock values parsed above."""
    return datetime.now(ZoneInfo('America/New_York')).replace(tzinfo=None)


def is_player_locked(conn, season, gw_number, club):
    """True once `club`'s fixture in this gw has kicked off."""
    kickoff = get_player_kickoff(conn, season, gw_number, club)
    return kickoff is not None and now_eastern_naive() >= kickoff


def get_player_lock_state(conn, season, gw_number, club):
    """'not_locked' | 'in_progress' | 'finished', based on this club's own
    kickoff -- distinct from is_player_locked's plain boolean so the UI can
    show "match still being played" separately from "match already over"
    instead of one identical 🔒 for both. The 2-hour assumed match length
    is a rough estimate (extra time/stoppages aren't accounted for), same
    spirit as is_gw_locked's 6-hour-after-last-kickoff gw-wide freeze."""
    kickoff = get_player_kickoff(conn, season, gw_number, club)
    if kickoff is None or now_eastern_naive() < kickoff:
        return 'not_locked'
    if now_eastern_naive() < kickoff + timedelta(hours=2):
        return 'in_progress'
    return 'finished'


def is_gw_locked(conn, season, gw_number):
    """True 6h after the gw's last kickoff — the whole gameweek is frozen,
    nothing about it (starters, bench, IR) changes again."""
    _, latest = get_gw_kickoff_bounds(conn, season, gw_number)
    return latest is not None and now_eastern_naive() >= latest + timedelta(hours=6)


def is_change_locked(conn, season, gw_number, club, old_slot_type, new_slot_type):
    """
    Shared lock check for both the slot-change endpoint and add/drop.
    Returns (locked: bool, reason: str-or-None).
    - Once the whole gw is locked (6h after its last kickoff), nothing about
      it can change at all.
    - Otherwise, a change that touches a 'starter' slot on either side (i.e.
      the player either currently is a starter, or is being made one) is
      blocked once that player's own club has kicked off — a locked starter
      can't be repositioned, benched, or dropped. Bench<->IR changes, and
      dropping a bench/IR player, remain allowed regardless of that
      player's own kickoff until the gw-wide lock kicks in.
    """
    if is_gw_locked(conn, season, gw_number):
        return True, f"GW{gw_number} is locked — the lineup is finalized."
    if 'starter' in (old_slot_type, new_slot_type) and is_player_locked(conn, season, gw_number, club):
        return True, "This player is locked — their match has already kicked off."
    return False, None


def build_fixture_day_groups(fixture_rows):
    """Group fixture rows by calendar day, sorted by date then kickoff time
    within the day. Rows with no match_date/kickoff_time yet (schedule not
    imported for this gameweek) land in a single trailing group with no date
    label, sorted by home_club like the old flat rendering."""
    scheduled = [r for r in fixture_rows if r['match_date'] and r['kickoff_time']]
    unscheduled = [r for r in fixture_rows if not (r['match_date'] and r['kickoff_time'])]

    scheduled.sort(key=lambda r: (r['match_date'], r['kickoff_time']))
    unscheduled.sort(key=lambda r: r['home_club'])

    groups = []
    current_date = None
    for r in scheduled:
        if r['match_date'] != current_date:
            current_date = r['match_date']
            date_obj = datetime.strptime(current_date, '%Y-%m-%d')
            groups.append({'label': date_obj.strftime('%A, %B %-d'), 'matches': []})
        _, time_label = format_kickoff(r['match_date'], r['kickoff_time'])
        groups[-1]['matches'].append({
            'match_id': r['match_id'], 'home_club': r['home_club'], 'away_club': r['away_club'], 'time': time_label
        })

    if unscheduled:
        groups.append({'label': None, 'matches': [
            {'match_id': r['match_id'], 'home_club': r['home_club'], 'away_club': r['away_club'], 'time': None}
            for r in unscheduled
        ]})

    return groups


def parse_scrape_gw_output(output):
    """Pull every RESULT_JSON line scrape_gw.py prints (one per GW, since a
    range like --gw 1 --gw_end 34 runs multiple) and merge them into one
    aggregate: totals plus a flat per-fixture detail list."""
    runs = []
    for line in output.splitlines():
        if line.startswith("RESULT_JSON:"):
            try:
                runs.append(json.loads(line[len("RESULT_JSON:"):]))
            except (ValueError, json.JSONDecodeError):
                pass

    agg = {
        "gws": [r["gw"] for r in runs],
        "total_fixtures": sum(r["total_fixtures"] for r in runs),
        "perfect": sum(len(r["perfect"]) for r in runs),
        "discrepancies": sum(len(r["discrepancies"]) for r in runs),
        "errors": sum(len(r["errors"]) for r in runs),
        "detail": {
            "perfect":       [x for r in runs for x in r["perfect"]],
            "discrepancies": [x for r in runs for x in r["discrepancies"]],
            "errors":        [x for r in runs for x in r["errors"]],
            "tab_failures":  [x for r in runs for x in r["tab_failures"]],
        },
    }
    return agg


def save_scraper_run(gw, gw_end, started_at, completed_at, status, agg, error_note=None, trigger='manual'):
    conn = get_db()
    conn.execute("""
        INSERT INTO scraper_runs (season, gw_start, gw_end, started_at, completed_at, status,
                                   total_fixtures, perfect_count, discrepancy_count, error_count,
                                   summary, detail_json, trigger)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        '2026-27', gw, gw_end or gw, started_at, completed_at, status,
        agg["total_fixtures"], agg["perfect"], agg["discrepancies"], agg["errors"],
        error_note or "", json.dumps(agg["detail"]), trigger,
    ))
    conn.commit()
    conn.close()


def run_scraper_background(proc, gw, started_at, trigger='manual', watchdog_killed=None):
    """
    Streams the already-launched scrape_gw.py's stdout line-by-line (Popen,
    not subprocess.run) so PROGRESS: lines can update scraper_status.json's
    `progress` field in near-real-time — the banner and Scraper Log page
    poll that file every 5s, otherwise a scrape runs ~15min with zero
    visibility. All other output is still buffered and parsed for the final
    RESULT_JSON: summary exactly as before; only the streaming mechanism
    changed, not the post-run reporting.

    `proc` is created by the caller (start_scrape), not here — its pid needs
    to land in the very first "running" status write so there's never a
    window where the status says running without a pid cancel can act on.

    `watchdog_killed` is the Event start_scrape's watchdog thread sets right
    before it force-kills a run that's overrun SCRAPER_STALE_MINUTES — used
    here only to report an accurate reason, not to do the killing itself.
    """
    completed_at = None
    output_lines = []
    stderr_lines = []
    try:
        def drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        for line in proc.stdout:
            output_lines.append(line)
            if line.startswith("PROGRESS:"):
                try:
                    progress = json.loads(line[len("PROGRESS:"):])
                except (ValueError, json.JSONDecodeError):
                    progress = None
                if progress:
                    status = read_scraper_status()
                    status["progress"] = progress
                    write_scraper_status(status)

        proc.wait()
        stderr_thread.join(timeout=5)

        output = "".join(output_lines)
        stderr_text = "".join(stderr_lines)
        returncode = proc.returncode
        completed_at = now_eastern_naive().strftime('%b %d, %Y at %-I:%M %p')
        agg = parse_scrape_gw_output(output)
        summary = {"perfect": str(agg["perfect"]), "discrepancies": str(agg["discrepancies"]), "errors": str(agg["errors"])}

        if returncode != 0:
            total = agg["total_fixtures"]
            if returncode < 0 and signal.Signals(-returncode) in (signal.SIGTERM, signal.SIGKILL):
                # Negative returncode means the process died from a signal
                # rather than exiting on its own — either the cancel
                # endpoint below, or the watchdog thread in start_scrape
                # force-killing a run that overran SCRAPER_STALE_MINUTES.
                if watchdog_killed is not None and watchdog_killed.is_set():
                    error_msg = f"❌ Scrape for GW{gw} ran past {SCRAPER_STALE_MINUTES} min and was force-killed to protect the server."
                else:
                    error_msg = f"❌ Cancelled by request for GW{gw}."
            elif total == 0:
                stderr_tail = stderr_text.strip().splitlines()
                reason = stderr_tail[-1] if stderr_tail else "no output — check server logs"
                error_msg = f"Scraper crashed before producing results for GW{gw}: {reason}"
            elif agg["errors"] == total:
                error_msg = f"{total}/{total} matches not played yet — nothing to scrape for GW{gw}."
            else:
                error_msg = f"{agg['errors']}/{total} matches failed to scrape for GW{gw}."
            save_scraper_run(gw, gw, started_at, completed_at, "error", agg, error_msg, trigger=trigger)
            write_scraper_status({
                "status":       "error",
                "error":        error_msg,
                "summary":      summary,
                "started_at":   started_at,
                "completed_at": completed_at,
                "gw":           gw,
            })
        else:
            save_scraper_run(gw, gw, started_at, completed_at, "complete", agg, trigger=trigger)
            try:
                fconn = get_db()
                try:
                    finalize_gw_results(fconn, gw, season=DRAFT_SEASON)
                finally:
                    fconn.close()
            except Exception as e:
                print(f"[finalize_gw_results] GW{gw} failed: {e}")
            write_scraper_status({
                "status":       "complete",
                "summary":      summary,
                "started_at":   started_at,
                "completed_at": completed_at,
                "gw":           gw,
            })
    except Exception as e:
        completed_at = completed_at or now_eastern_naive().strftime('%b %d, %Y at %-I:%M %p')
        write_scraper_status({
            "status":       "error",
            "error":        f"❌ {e}",
            "started_at":   started_at,
            "completed_at": completed_at,
            "gw":           gw,
        })


# ── Scraper audit log ────────────────────────────────────────────────────────

SCRAPER_TIMESTAMP_FMT = '%b %d, %Y at %-I:%M %p'


def format_duration(started_at, completed_at):
    """Human-readable duration between two SCRAPER_TIMESTAMP_FMT strings, e.g.
    '<1 min', '14 min', '1h 3m'. Minute-precision only, matching the
    precision of the stored timestamps themselves. None if either is
    missing/unparseable (e.g. a run that's still going, or an old row from
    before this format existed)."""
    if not started_at or not completed_at:
        return None
    try:
        start = datetime.strptime(started_at, SCRAPER_TIMESTAMP_FMT)
        end = datetime.strptime(completed_at, SCRAPER_TIMESTAMP_FMT)
    except ValueError:
        return None
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1:
        return '<1 min'
    if minutes < 60:
        return f'{minutes} min'
    return f'{minutes // 60}h {minutes % 60}m'


@app.route('/scraper-log')
def scraper_log():
    conn = get_db()
    runs = conn.execute("""
        SELECT * FROM scraper_runs ORDER BY id DESC LIMIT 50
    """).fetchall()
    conn.close()

    runs_out = []
    for r in runs:
        row = dict(r)
        row['detail'] = json.loads(row['detail_json'])
        row['duration'] = format_duration(row.get('started_at'), row.get('completed_at'))
        runs_out.append(row)

    return render_template('scraper_log.html', runs=runs_out, scraper_status=read_scraper_status())


# ── Transfer Room ────────────────────────────────────────────────────────────

@app.route('/transfers')
def transfers_page():
    conn = get_db()
    journalists = conn.execute("""
        SELECT * FROM transfer_journalists WHERE status='active' ORDER BY name
    """).fetchall()
    pending = conn.execute("""
        SELECT * FROM transfer_journalists WHERE status='pending' ORDER BY added_at
    """).fetchall()
    declined = conn.execute("""
        SELECT * FROM transfer_journalists WHERE status='rejected' ORDER BY reviewed_at DESC LIMIT 10
    """).fetchall()
    managers = conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()
    conn.close()
    return render_template('transfers.html', journalists=journalists, pending=pending,
                            declined=declined, managers=managers, transfer_list_url=TRANSFER_LIST_URL)


@app.route('/transfers/journalists/propose', methods=['POST'])
def propose_transfer_journalist():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    x_handle = (data.get('x_handle') or '').strip().lstrip('@')
    notes = (data.get('notes') or '').strip()

    if not name or not x_handle:
        return jsonify({"error": "Name and X handle are required"}), 400

    conn = get_db()
    proposer = conn.execute("SELECT name FROM managers WHERE id=?", (current_manager_id(),)).fetchone()
    proposed_by = proposer['name'] if proposer else 'Unknown'
    conn.execute("""
        INSERT INTO transfer_journalists (name, x_handle, notes, added_at, status, proposed_by)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (name, x_handle, notes, now_eastern_naive().isoformat(), proposed_by))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/transfers/journalists/<int:journalist_id>/approve', methods=['POST'])
def approve_transfer_journalist(journalist_id):
    conn = get_db()
    conn.execute("""
        UPDATE transfer_journalists SET status='active', reviewed_at=? WHERE id=?
    """, (now_eastern_naive().isoformat(), journalist_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/transfers/journalists/<int:journalist_id>/reject', methods=['POST'])
def reject_transfer_journalist(journalist_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({"error": "A reason is required"}), 400

    conn = get_db()
    conn.execute("""
        UPDATE transfer_journalists SET status='rejected', review_note=?, reviewed_at=? WHERE id=?
    """, (reason, now_eastern_naive().isoformat(), journalist_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Memes / Locker Room ──────────────────────────────────────────────────────

MEME_LINK_TIMEOUT = 6
MEME_FEED_PAGE_SIZE = 15

YOUTUBE_RE = re.compile(r'(?:youtube\.com/watch\?v=|youtube\.com/shorts/|youtu\.be/)([\w-]{11})')
IMAGE_EXT_RE = re.compile(r'\.(jpg|jpeg|png|gif|webp)(\?.*)?$', re.I)


def _meme_http_get(url, timeout=MEME_LINK_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; FutbolFantasiaBot/1.0)'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000).decode('utf-8', errors='replace')


class _OGTagParser(HTMLParser):
    """Minimal Open Graph tag scraper — stdlib only, no BeautifulSoup dependency."""
    def __init__(self):
        super().__init__()
        self.og = {}
        self.title = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            prop = attrs.get('property') or attrs.get('name')
            if prop in ('og:title', 'og:image', 'og:description') and attrs.get('content'):
                self.og[prop] = attrs['content']
        elif tag == 'title':
            self._in_title = True

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False


def _meme_oembed(oembed_url):
    try:
        body = _meme_http_get(oembed_url)
        data = json.loads(body)
        return data.get('html')
    except Exception:
        return None


def _meme_og_fallback(url, link_type):
    import html as html_module
    try:
        body = _meme_http_get(url)
        parser = _OGTagParser()
        parser.feed(body)
        title = html_module.escape(parser.og.get('og:title') or parser.title or url)
        image = html_module.escape(parser.og.get('og:image', ''), quote=True)
        description = html_module.escape(parser.og.get('og:description') or '')
        safe_url = html_module.escape(url, quote=True)
        img_html = f'<img src="{image}" class="link-preview-img" />' if image else ''
        card = (
            f'<div class="link-preview-card">{img_html}'
            f'<div class="link-preview-body">'
            f'<div class="link-preview-title">{title}</div>'
            f'<div class="link-preview-desc">{description}</div>'
            f'<a href="{safe_url}" target="_blank" rel="noopener" class="link-preview-url">🔗 Open link</a>'
            f'</div></div>'
        )
        return link_type, card
    except Exception:
        return link_type, None


def detect_link_embed(url):
    """
    Returns (link_type, embed_html) for a pasted URL. embed_html is None
    when everything fails and the post should just show a plain link.
    Never raises - every branch degrades gracefully.
    """
    import html as html_module
    host = (urllib.parse.urlparse(url).hostname or '').lower()

    m = YOUTUBE_RE.search(url)
    if m and ('youtube.com' in host or 'youtu.be' in host):
        video_id = m.group(1)
        embed_html = (
            f'<div class="video-embed-wrap"><iframe src="https://www.youtube.com/embed/{video_id}" '
            f'frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
            f'gyroscope; picture-in-picture" allowfullscreen></iframe></div>'
        )
        return 'youtube', embed_html

    if 'imgur.com' in host and IMAGE_EXT_RE.search(url):
        safe_url = html_module.escape(url, quote=True)
        return 'imgur_direct', f'<img src="{safe_url}" class="meme-link-img" />'

    if 'twitter.com' in host or 'x.com' in host:
        html_out = _meme_oembed('https://publish.twitter.com/oembed?url=' + urllib.parse.quote(url, safe=''))
        if html_out:
            return 'twitter', html_out
        return _meme_og_fallback(url, 'twitter')

    if 'reddit.com' in host:
        html_out = _meme_oembed('https://www.reddit.com/oembed?url=' + urllib.parse.quote(url, safe=''))
        if html_out:
            return 'reddit', html_out
        return _meme_og_fallback(url, 'reddit')

    if 'tiktok.com' in host:
        html_out = _meme_oembed('https://www.tiktok.com/oembed?url=' + urllib.parse.quote(url, safe=''))
        if html_out:
            return 'tiktok', html_out
        return _meme_og_fallback(url, 'tiktok')

    if 'imgur.com' in host:
        html_out = _meme_oembed('https://api.imgur.com/oembed?url=' + urllib.parse.quote(url, safe=''))
        if html_out:
            return 'imgur', html_out
        return _meme_og_fallback(url, 'imgur')

    return _meme_og_fallback(url, 'link')


def _meme_post_out(row, current_manager_id=None):
    """Shape a meme_posts row (+ reactions/comments) for JSON/template use."""
    return {
        'id': row['id'],
        'manager_id': row['manager_id'],
        'manager_name': row['manager_name'],
        'team_name': row['team_name'],
        'post_type': row['post_type'],
        'image_path': row['image_path'],
        'link_url': row['link_url'],
        'link_type': row['link_type'],
        'embed_html': row['embed_html'],
        'caption': row['caption'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'edited': row['updated_at'] != row['created_at'],
    }


def _fetch_meme_posts(conn, before=None, before_id=None, limit=MEME_FEED_PAGE_SIZE):
    where = ""
    params = []
    if before is not None and before_id is not None:
        where = "WHERE (mp.updated_at < ?) OR (mp.updated_at = ? AND mp.id < ?)"
        params = [before, before, before_id]
    rows = conn.execute(f"""
        SELECT mp.*, m.name AS manager_name, m.team_name AS team_name
        FROM meme_posts mp
        JOIN managers m ON m.id = mp.manager_id
        {where}
        ORDER BY mp.updated_at DESC, mp.id DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    post_ids = [r['id'] for r in rows]
    reactions_by_post = {}
    comments_by_post = {}
    if post_ids:
        placeholders = ','.join('?' * len(post_ids))
        for r in conn.execute(f"""
            SELECT mr.post_id, mr.emoji, mr.manager_id, m.name AS manager_name
            FROM meme_reactions mr JOIN managers m ON m.id = mr.manager_id
            WHERE mr.post_id IN ({placeholders})
        """, post_ids).fetchall():
            reactions_by_post.setdefault(r['post_id'], []).append(dict(r))
        for r in conn.execute(f"""
            SELECT mc.*, m.name AS manager_name
            FROM meme_comments mc JOIN managers m ON m.id = mc.manager_id
            WHERE mc.post_id IN ({placeholders})
            ORDER BY mc.created_at ASC
        """, post_ids).fetchall():
            comments_by_post.setdefault(r['post_id'], []).append(dict(r))

    out = []
    for r in rows:
        post = _meme_post_out(r)
        post['reactions'] = reactions_by_post.get(r['id'], [])
        post['comments'] = comments_by_post.get(r['id'], [])
        out.append(post)
    return out


@app.route('/memes')
def memes_page():
    conn = get_db()
    posts = _fetch_meme_posts(conn)
    managers = [dict(r) for r in conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()]
    conn.close()
    has_more = len(posts) == MEME_FEED_PAGE_SIZE
    return render_template('memes.html', posts=posts, managers=managers, has_more=has_more)


@app.route('/memes/feed')
def memes_feed():
    before = request.args.get('before')
    before_id = request.args.get('before_id', type=int)
    conn = get_db()
    posts = _fetch_meme_posts(conn, before=before, before_id=before_id)
    conn.close()
    has_more = len(posts) == MEME_FEED_PAGE_SIZE
    return jsonify({"posts": posts, "has_more": has_more})


def _save_meme_image_file(file_storage):
    filename = file_storage.filename or ''
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_MEME_EXTS:
        return None, "Unsupported file type. Use PNG, JPG, GIF, or WEBP."
    os.makedirs(MEMES_DIR, exist_ok=True)
    saved_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    file_storage.save(os.path.join(MEMES_DIR, saved_name))
    return f"uploads/memes/{saved_name}", None


MEME_IMAGE_CONTENT_TYPES = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/jpg': 'jpg',
    'image/gif': 'gif', 'image/webp': 'webp',
}
MEME_IMAGE_FETCH_MAX_BYTES = 8 * 1024 * 1024


@app.route('/memes/canvas/fetch-image', methods=['POST'])
def fetch_canvas_image():
    """
    Server-side fetch for the canvas editor's "add image by URL" feature.
    Downloading and re-hosting the image ourselves (rather than loading the
    remote URL directly into the canvas) avoids the CORS "tainted canvas"
    problem — a canvas that ever painted a cross-origin image without
    permissive CORS headers throws on toDataURL(), which would silently
    break every meme save. Serving our own copy back same-origin sidesteps
    that entirely.
    """
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    if not url.lower().startswith(('http://', 'https://')):
        return jsonify({"error": "Invalid URL"}), 400

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; FutbolFantasiaBot/1.0)'})
        with urllib.request.urlopen(req, timeout=MEME_LINK_TIMEOUT) as resp:
            content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            ext = MEME_IMAGE_CONTENT_TYPES.get(content_type)
            if not ext:
                return jsonify({"error": "That URL doesn't point to a supported image (PNG/JPG/GIF/WEBP)."}), 400
            body = resp.read(MEME_IMAGE_FETCH_MAX_BYTES + 1)
            if len(body) > MEME_IMAGE_FETCH_MAX_BYTES:
                return jsonify({"error": "Image is too large (8MB max)."}), 400
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return jsonify({"error": "Couldn't fetch that image URL."}), 400

    os.makedirs(MEMES_DIR, exist_ok=True)
    saved_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    with open(os.path.join(MEMES_DIR, saved_name), 'wb') as f:
        f.write(body)

    return jsonify({"status": "ok", "path": f"uploads/memes/{saved_name}"})


@app.route('/memes/new', methods=['POST'])
def create_meme_post():
    manager_id = current_manager_id()
    caption = (request.form.get('caption') or '').strip()
    link_url = (request.form.get('link_url') or '').strip()
    image_file = request.files.get('image')

    if not image_file and not link_url:
        return jsonify({"error": "Provide an image or a link"}), 400

    conn = get_db()
    now = now_eastern_naive().isoformat()

    if image_file and image_file.filename:
        image_path, err = _save_meme_image_file(image_file)
        if err:
            conn.close()
            return jsonify({"error": err}), 400
        conn.execute("""
            INSERT INTO meme_posts (manager_id, post_type, image_path, caption, created_at, updated_at)
            VALUES (?, 'image', ?, ?, ?, ?)
        """, (manager_id, image_path, caption, now, now))
    else:
        link_type, embed_html = detect_link_embed(link_url)
        conn.execute("""
            INSERT INTO meme_posts (manager_id, post_type, link_url, link_type, embed_html, caption, created_at, updated_at)
            VALUES (?, 'link', ?, ?, ?, ?, ?, ?)
        """, (manager_id, link_url, link_type, embed_html, caption, now, now))

    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
    conn.close()
    return jsonify({"status": "ok", "id": new_id})


@app.route('/memes/<int:post_id>/edit', methods=['POST'])
def edit_meme_post(post_id):
    manager_id = current_manager_id()
    caption = request.form.get('caption')
    link_url = (request.form.get('link_url') or '').strip()
    image_file = request.files.get('image')

    conn = get_db()
    post = conn.execute("SELECT * FROM meme_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({"error": "Post not found"}), 404
    if str(post['manager_id']) != str(manager_id):
        conn.close()
        return jsonify({"error": "Only the original poster can edit this post"}), 403

    now = now_eastern_naive().isoformat()
    updates = {'caption': caption if caption is not None else post['caption'], 'updated_at': now}

    if post['post_type'] == 'image' and image_file and image_file.filename:
        image_path, err = _save_meme_image_file(image_file)
        if err:
            conn.close()
            return jsonify({"error": err}), 400
        updates['image_path'] = image_path
    elif post['post_type'] == 'link' and link_url and link_url != post['link_url']:
        link_type, embed_html = detect_link_embed(link_url)
        updates['link_url'] = link_url
        updates['link_type'] = link_type
        updates['embed_html'] = embed_html

    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE meme_posts SET {set_clause} WHERE id=?", list(updates.values()) + [post_id])
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/memes/<int:post_id>/delete', methods=['POST'])
def delete_meme_post(post_id):
    manager_id = current_manager_id()

    conn = get_db()
    post = conn.execute("SELECT * FROM meme_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({"error": "Post not found"}), 404
    if str(post['manager_id']) != str(manager_id):
        conn.close()
        return jsonify({"error": "Only the original poster can delete this post"}), 403

    conn.execute("DELETE FROM meme_reactions WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM meme_comments WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM meme_posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()

    if post['image_path']:
        full_path = os.path.join(os.path.dirname(__file__), 'static', post['image_path'])
        if os.path.exists(full_path):
            os.remove(full_path)

    return jsonify({"status": "ok"})


@app.route('/memes/<int:post_id>/react', methods=['POST'])
def react_to_meme_post(post_id):
    data = request.get_json() or {}
    manager_id = current_manager_id()
    emoji = data.get('emoji')
    if not emoji:
        return jsonify({"error": "Missing emoji"}), 400

    conn = get_db()
    existing = conn.execute("""
        SELECT id FROM meme_reactions WHERE post_id=? AND manager_id=? AND emoji=?
    """, (post_id, manager_id, emoji)).fetchone()

    if existing:
        conn.execute("DELETE FROM meme_reactions WHERE id=?", (existing['id'],))
        toggled_on = False
    else:
        conn.execute("""
            INSERT INTO meme_reactions (post_id, manager_id, emoji, created_at) VALUES (?, ?, ?, ?)
        """, (post_id, manager_id, emoji, now_eastern_naive().isoformat()))
        toggled_on = True

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "on": toggled_on})


@app.route('/memes/<int:post_id>/comment', methods=['POST'])
def comment_on_meme_post(post_id):
    data = request.get_json() or {}
    manager_id = current_manager_id()
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({"error": "Missing comment body"}), 400

    conn = get_db()
    conn.execute("""
        INSERT INTO meme_comments (post_id, manager_id, body, created_at) VALUES (?, ?, ?, ?)
    """, (post_id, manager_id, body, now_eastern_naive().isoformat()))
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
    manager = conn.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "id": new_id, "manager_name": manager['name'] if manager else ''})


@app.route('/memes/comment/<int:comment_id>/delete', methods=['POST'])
def delete_meme_comment(comment_id):
    manager_id = current_manager_id()

    conn = get_db()
    comment = conn.execute("SELECT * FROM meme_comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        conn.close()
        return jsonify({"error": "Comment not found"}), 404
    if str(comment['manager_id']) != str(manager_id):
        conn.close()
        return jsonify({"error": "Only the original commenter can delete this comment"}), 403

    conn.execute("DELETE FROM meme_comments WHERE id=?", (comment_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Login ────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET'])
def login():
    conn = get_db()
    managers = conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()

    selected = request.args.get('manager_id', type=int)
    selected_manager = None
    needs_pin_setup = False
    if selected:
        row = conn.execute("SELECT id, name, team_name, pin_hash FROM managers WHERE id=?", (selected,)).fetchone()
        if row:
            selected_manager = row
            needs_pin_setup = row['pin_hash'] is None
    conn.close()

    return render_template('login.html', managers=managers,
                            selected_manager=selected_manager, needs_pin_setup=needs_pin_setup)


@app.route('/login', methods=['POST'])
def login_submit():
    data = request.get_json(silent=True) or {}
    try:
        manager_id = int(data.get('manager_id'))
    except (TypeError, ValueError):
        return jsonify({"error": "Select a manager."}), 400
    pin = (data.get('pin') or '').strip()
    confirm_pin = (data.get('confirm_pin') or '').strip()

    conn = get_db()
    row = conn.execute("SELECT id, name, pin_hash FROM managers WHERE id=?", (manager_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Unknown manager."}), 400

    if not pin:
        conn.close()
        return jsonify({"error": "Enter a PIN."}), 400

    if row['pin_hash'] is None:
        # First-time setup — re-check pin_hash is still NULL server-side
        # (don't trust a client-submitted "which branch" flag) so nobody
        # can silently overwrite a friend's PIN once it's been claimed.
        if pin != confirm_pin:
            conn.close()
            return jsonify({"error": "PINs don't match."}), 400
        if len(pin) < 4:
            conn.close()
            return jsonify({"error": "PIN must be at least 4 characters."}), 400
        conn.execute("UPDATE managers SET pin_hash=? WHERE id=? AND pin_hash IS NULL",
                     (generate_password_hash(pin), manager_id))
        if conn.total_changes == 0:
            conn.close()
            return jsonify({"error": "This manager already has a PIN set — enter it instead."}), 409
        conn.commit()
        conn.close()
    else:
        if not check_password_hash(row['pin_hash'], pin):
            conn.close()
            return jsonify({"error": "Incorrect PIN."}), 401
        conn.close()

    session.clear()
    session['manager_id'] = manager_id
    session.permanent = True
    return jsonify({"status": "ok", "redirect": url_for('standings')})


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Standings ────────────────────────────────────────────────────────────────

@app.route('/')
def standings():
    conn = get_db()
    c    = conn.cursor()
    season = '2026-27'

    standings_rows = c.execute("""
        SELECT
            m.id, m.name, m.team_name, m.photo_path,
            COALESCE(SUM(res.win),           0)   AS wins,
            COALESCE(SUM(res.loss),          0)   AS losses,
            COALESCE(SUM(res.tie),           0)   AS ties,
            COALESCE(SUM(res.fantasy_score), 0.0) AS pts_for,
            COALESCE(SUM(opp.fantasy_score), 0.0) AS pts_against
        FROM managers m
        LEFT JOIN (
            SELECT r.* FROM results r
            JOIN gameweeks g ON g.id = r.gw_id
            WHERE g.season = ?
        ) res ON res.manager_id = m.id
        LEFT JOIN results opp
            ON  opp.matchup_id  = res.matchup_id
            AND opp.manager_id != m.id
        GROUP BY m.id, m.name, m.team_name, m.photo_path
        ORDER BY wins DESC, pts_for DESC
    """, (season,)).fetchall()

    glance_rows = c.execute("""
        SELECT g.gw_number, res.manager_id,
               res.win, res.loss, res.tie, res.fantasy_score
        FROM results res
        JOIN gameweeks g ON g.id = res.gw_id AND g.season = ?
        ORDER BY g.gw_number, res.manager_id
    """, (season,)).fetchall()

    managers = c.execute(
        "SELECT id, name, team_name, photo_path FROM managers ORDER BY name"
    ).fetchall()

    glance = {}
    for row in glance_rows:
        glance.setdefault(row['gw_number'], {})[row['manager_id']] = row

    # ── Top Scorer: best-scoring player per manager for a given gw ─────────
    completed_gws = sorted({row['gw_number'] for row in glance_rows})

    ts_gw = request.args.get('ts_gw', type=int)
    if ts_gw not in completed_gws:
        ts_gw = completed_gws[-1] if completed_gws else None

    top_scorers = {}
    if ts_gw:
        rules_row = c.execute(
            "SELECT COUNT(*) FROM scoring_config WHERE season=?", (season,)
        ).fetchone()
        scoring_season = season if rules_row and rules_row[0] > 0 else "2025-26"

        for m in managers:
            roster = get_roster_at_gw(conn, m['id'], ts_gw, season, scoring_season)
            eligible = [r for r in roster if r['slot_type'] in ('starter', 'bench')]
            if eligible:
                best = max(eligible, key=lambda r: r['gw_score'])
                top_scorers[m['id']] = {
                    'player_name': best['player_name'],
                    'gw_score': best['gw_score'],
                    'is_bench': best['slot_type'] == 'bench',
                }

    conn.close()

    return render_template('standings.html',
        standings=standings_rows,
        managers=managers,
        gws=list(range(1, 34)),
        glance=glance,
        season=season,
        scraper_status=read_scraper_status(),
        completed_gws=completed_gws,
        ts_gw=ts_gw,
        top_scorers=top_scorers,
    )


# ── Gameweek ─────────────────────────────────────────────────────────────────

@app.route('/gameweek')
@app.route('/gameweek/<int:gw>')
def gameweek(gw=None):
    conn   = get_db()
    c      = conn.cursor()
    season = '2026-27'
    badges = load_badges()

    if gw is None:
        gw = get_current_gw(conn, season)

    gw = max(1, min(gw, 33))

    matchups = c.execute("""
        SELECT
            mu.id,
            ma.id        AS team_a_id,
            ma.name      AS team_a_name,
            ma.team_name AS team_a_team,
            ma.photo_path AS team_a_photo,
            mb.id        AS team_b_id,
            mb.name      AS team_b_name,
            mb.team_name AS team_b_team,
            mb.photo_path AS team_b_photo,
            ra.fantasy_score AS score_a,
            ra.win AS win_a, ra.loss AS loss_a, ra.tie AS tie_a,
            rb.fantasy_score AS score_b,
            rb.win AS win_b, rb.loss AS loss_b, rb.tie AS tie_b
        FROM matchups mu
        JOIN managers ma ON ma.id = mu.team_a_id
        JOIN managers mb ON mb.id = mu.team_b_id
        LEFT JOIN results ra
            ON ra.matchup_id = mu.id AND ra.manager_id = mu.team_a_id
        LEFT JOIN results rb
            ON rb.matchup_id = mu.id AND rb.manager_id = mu.team_b_id
        WHERE mu.season = ? AND mu.gw_number = ?
        ORDER BY ma.name
    """, (season, gw)).fetchall()

    # Official scores only exist once finalize_gw_results has run for the
    # whole gw (every fixture scraped) -- until then, show a live/interim
    # total computed straight from whatever raw_stats exist so far, clearly
    # marked as not-yet-final (no win/loss/tie implied). Only bother once
    # this gw actually has *some* scraped data -- otherwise every future,
    # not-yet-played gw would show a noisy "LIVE 0.00" for every matchup.
    matchups = [dict(m) for m in matchups]
    gw_has_any_data = c.execute(
        "SELECT 1 FROM raw_stats WHERE gw_number=? AND external=0 LIMIT 1", (gw,)
    ).fetchone() is not None
    for m in matchups:
        m['live_score_a'] = m['live_score_b'] = None
        if gw_has_any_data:
            if m['score_a'] is None:
                m['live_score_a'], _ = calc_team_score_for_gw(conn, m['team_a_id'], gw, season=season)
            if m['score_b'] is None:
                m['live_score_b'], _ = calc_team_score_for_gw(conn, m['team_b_id'], gw, season=season)

    fixture_rows = c.execute("""
        SELECT f.match_id, f.home_club, f.away_club, f.match_date, f.kickoff_time
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ?
        ORDER BY f.home_club
    """, (gw, season)).fetchall()

    day_groups = build_fixture_day_groups(fixture_rows)

    managers = c.execute("SELECT id, name FROM managers ORDER BY name").fetchall()

    fully_scraped = gw_fully_scraped(conn, season, gw)
    already_finalized = c.execute("""
        SELECT 1 FROM results r JOIN gameweeks g ON g.id = r.gw_id
        WHERE g.season=? AND g.gw_number=? LIMIT 1
    """, (season, gw)).fetchone() is not None
    top_scorer = get_gw_top_scorer(conn, season, gw) if already_finalized else None

    conn.close()

    return render_template('gameweek.html',
        matchups=matchups,
        day_groups=day_groups,
        gw=gw,
        prev_gw=gw - 1 if gw > 1  else None,
        next_gw=gw + 1 if gw < 33 else None,
        season=season,
        scraper_status=read_scraper_status(),
        badges=badges,
        managers=managers,
        fully_scraped=fully_scraped,
        already_finalized=already_finalized,
        top_scorer=top_scorer,
    )


# ── Teams ────────────────────────────────────────────────────────────────────

@app.route('/team')
def team_select():
    conn = get_db()
    managers = conn.execute(
        "SELECT id, name, team_name, photo_path FROM managers ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template('team_select.html',
        managers=managers,
        season='2026-27',
        scraper_status=read_scraper_status(),
    )


@app.route('/team/<int:manager_id>')
def team(manager_id):
    conn   = get_db()
    c      = conn.cursor()
    season = '2026-27'
    badges = load_badges()

    manager = c.execute(
        "SELECT id, name, team_name, photo_path FROM managers WHERE id=?", (manager_id,)
    ).fetchone()
    if not manager:
        conn.close()
        return "Manager not found", 404

    all_managers = c.execute(
        "SELECT id, name, team_name, photo_path FROM managers ORDER BY name"
    ).fetchall()

    current_gw = get_current_gw(conn, season)

    matchup = c.execute("""
        SELECT
            mu.id AS matchup_id,
            CASE WHEN mu.team_a_id=? THEN mb.name      ELSE ma.name      END AS opp_name,
            CASE WHEN mu.team_a_id=? THEN mb.team_name ELSE ma.team_name END AS opp_team,
            CASE WHEN mu.team_a_id=? THEN mb.id        ELSE ma.id        END AS opp_id,
            ra.fantasy_score AS my_score,
            rb.fantasy_score AS opp_score,
            ra.win AS my_win, ra.loss AS my_loss, ra.tie AS my_tie
        FROM matchups mu
        JOIN managers ma ON ma.id = mu.team_a_id
        JOIN managers mb ON mb.id = mu.team_b_id
        LEFT JOIN results ra
            ON ra.matchup_id = mu.id AND ra.manager_id = ?
        LEFT JOIN results rb
            ON rb.matchup_id = mu.id AND rb.manager_id != ?
        WHERE mu.season=? AND mu.gw_number=?
          AND (mu.team_a_id=? OR mu.team_b_id=?)
    """, (manager_id,)*3 + (manager_id, manager_id, season, current_gw,
                             manager_id, manager_id)).fetchone()

    # Official scores only exist once finalize_gw_results has run for the
    # whole gw -- until then, show a live/interim total the same way the
    # Gameweek page's matchup cards already do, once this gw has any
    # scraped data at all (avoids a noisy "LIVE 0.00" on an unplayed gw).
    if matchup:
        matchup = dict(matchup)
        matchup['live_my_score'] = matchup['live_opp_score'] = None
        gw_has_any_data = c.execute(
            "SELECT 1 FROM raw_stats WHERE gw_number=? AND external=0 LIMIT 1", (current_gw,)
        ).fetchone() is not None
        if gw_has_any_data:
            if matchup['my_score'] is None:
                matchup['live_my_score'], _ = calc_team_score_for_gw(conn, manager_id, current_gw, season=season)
            if matchup['opp_score'] is None:
                matchup['live_opp_score'], _ = calc_team_score_for_gw(conn, matchup['opp_id'], current_gw, season=season)

    # ── Head-to-Head: full-season schedule vs. every opponent ──────────────
    # matchups already holds the complete pre-generated 33-GW schedule (see
    # scripts/generate_schedule.py), so this is a pure read -- no
    # "not generated yet" case to handle, only "not yet played" (no results
    # row for that matchup_id).
    h2h_rows = c.execute("""
        SELECT
            mu.gw_number,
            CASE WHEN mu.team_a_id=? THEN mu.team_b_id ELSE mu.team_a_id END AS opp_id,
            ra.fantasy_score AS my_score, rb.fantasy_score AS opp_score,
            ra.win AS my_win, ra.loss AS my_loss, ra.tie AS my_tie
        FROM matchups mu
        LEFT JOIN results ra ON ra.matchup_id = mu.id AND ra.manager_id = ?
        LEFT JOIN results rb ON rb.matchup_id = mu.id AND rb.manager_id != ?
        WHERE mu.season = ? AND (mu.team_a_id = ? OR mu.team_b_id = ?)
        ORDER BY mu.gw_number
    """, (manager_id, manager_id, manager_id, season, manager_id, manager_id)).fetchall()

    managers_by_id = {m['id']: m for m in all_managers}
    h2h = {}
    for row in h2h_rows:
        opp_id = row['opp_id']
        entry = h2h.setdefault(opp_id, {
            'opponent': managers_by_id.get(opp_id),
            'wins': 0, 'losses': 0, 'ties': 0, 'remaining': 0, 'meetings': [],
        })
        played = row['my_score'] is not None
        if played:
            if row['my_win']:
                entry['wins'] += 1
                result = 'win'
            elif row['my_loss']:
                entry['losses'] += 1
                result = 'loss'
            else:
                entry['ties'] += 1
                result = 'tie'
        else:
            entry['remaining'] += 1
            result = None
        entry['meetings'].append({
            'gw_number': row['gw_number'],
            'my_score': row['my_score'],
            'opp_score': row['opp_score'],
            'result': result,
        })

    roster_rows = c.execute("""
        SELECT r.player_name, r.slot_type, r.position_slot,
               p.club, p.position
        FROM rosters r
        LEFT JOIN players p ON p.name = r.player_name
        WHERE r.manager_id=?
          AND r.gw_start <= ?
          AND (r.gw_end IS NULL OR r.gw_end >= ?)
        ORDER BY r.slot_type DESC, r.position_slot
    """, (manager_id, current_gw, current_gw)).fetchall()

    player_names = [r['player_name'] for r in roster_rows]

    # Club must come from the live players.club join already on roster_rows
    # (p.club, above) — not from raw_stats. raw_stats rows are tagged with
    # whatever club a player was on for that historical match, so for anyone
    # who has since transferred it silently returns their OLD club (West Ham
    # for a player now at Tottenham), and for a brand-new-to-the-league
    # signing with only backfilled foreign-league history it can return a
    # club that was never even in the Premier League (e.g. "Braga"). This is
    # also what plan_club_map (below) and history()/draft_page() already do
    # correctly — this was the one place still reading the stale source.
    club_map = {r['player_name']: r['club'] for r in roster_rows}

    # Eligibility map: player_name -> sorted list of eligible position codes
    eligibility_map = {}
    if player_names:
        ph = ','.join('?' * len(player_names))
        elig_rows = c.execute(f"""
            SELECT p.name, pe.position
            FROM players p
            JOIN player_eligibility pe ON pe.player_id = p.id
            WHERE p.name IN ({ph})
        """, player_names).fetchall()
        for r in elig_rows:
            eligibility_map.setdefault(r['name'], set()).add(r['position'])
        eligibility_map = {name: sorted(positions) for name, positions in eligibility_map.items()}

    season_fixtures = c.execute("""
        SELECT g.gw_number, f.match_id, f.home_club, f.away_club
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE f.season=? AND g.gw_number <= ?
        ORDER BY g.gw_number
    """, (season, current_gw)).fetchall()

    club_matches = {}
    for row in season_fixtures:
        for club in (row['home_club'], row['away_club']):
            club_matches.setdefault(club, []).append((row['gw_number'], row['match_id']))

    fixture_info = get_gw_fixture_info(conn, season, current_gw)
    opponent_map = {club: info['opponent'] for club, info in fixture_info.items()}

    rules_row = c.execute(
        "SELECT COUNT(*) FROM scoring_config WHERE season=?", (season,)
    ).fetchone()
    scoring_season = season if rules_row and rules_row[0] > 0 else "2025-26"

    points_map = {}
    for player in roster_rows:
        name  = player['player_name']
        pos   = resolve_scoring_position(player['position_slot'], player['position'])
        club  = club_map.get(name)
        matches = club_matches.get(club, []) if club else []

        gw_score = 0.0
        season_total = 0.0
        games_played = 0

        for gw_num, match_id in matches:
            score, _ = calc_player_score(conn, name, match_id, pos, season=scoring_season)
            if score:
                games_played += 1
            season_total += score
            if gw_num == current_gw:
                gw_score = score

        season_avg = round(season_total / games_played, 2) if games_played else 0.0

        points_map[name] = {
            'gw_score': round(gw_score, 2),
            'season_total': round(season_total, 2),
            'season_avg': season_avg,
        }

    season_match_ids = [r['match_id'] for r in season_fixtures if r['gw_number'] == current_gw]

    stats_map = {}
    if player_names and season_match_ids:
        ph_p = ','.join('?' * len(player_names))
        ph_m = ','.join('?' * len(season_match_ids))
        stats_rows = c.execute(f"""
            SELECT player_name,
                   SUM(goals)          AS goals,
                   SUM(assists)        AS assists,
                   SUM(saves)          AS saves,
                   SUM(yellow_cards)   AS yellows,
                   SUM(red_cards)      AS reds,
                   SUM(minutes_played) AS minutes,
                   SUM(shots_on_target)AS sot,
                   SUM(key_passes)     AS kp,
                   SUM(tackles)        AS tackles,
                   SUM(pk_saves)       AS pk_saves,
                   SUM(glc)            AS glc,
                   SUM(motm)           AS motm,
                   SUM(own_goals)      AS own_goals,
                   SUM(lmt)            AS lmt,
                   SUM(elg)            AS elg,
                   SUM(dribbles)       AS dribbles,
                   SUM(interceptions)  AS interceptions,
                   SUM(clearances)     AS clearances,
                   SUM(blocked_shots)  AS blocked_shots,
                   SUM(acc_crosses)    AS acc_crosses,
                   SUM(acc_long_balls) AS acc_long_balls
            FROM raw_stats
            WHERE match_id IN ({ph_m}) AND player_name IN ({ph_p})
            GROUP BY player_name
        """, season_match_ids + player_names).fetchall()
        stats_map = {r['player_name']: dict(r) for r in stats_rows}

    # Clean sheet / goals conceded for this gw's DEF/GK -- mirrors
    # calc_player_score's own rule (60+ mins, 0 conceded, non-external row)
    # exactly, so the display matches what actually got scored.
    for player in roster_rows:
        name = player['player_name']
        pos = (player['position_slot'] or '').upper()
        s = stats_map.get(name)
        if pos not in ('DEF', 'GK') or not s:
            continue
        club = club_map.get(name)
        match_id = fixture_info.get(club, {}).get('match_id') if club else None
        if not match_id:
            continue
        ext_row = c.execute(
            "SELECT external FROM raw_stats WHERE match_id=? AND player_name=?", (match_id, name)
        ).fetchone()
        if ext_row and ext_row['external']:
            continue
        conceded = get_team_goals_conceded(conn, match_id, club)
        s['goals_conceded'] = conceded
        s['clean_sheet'] = 1 if (s.get('minutes') and s['minutes'] >= 60 and conceded == 0) else 0

    POS_ORDER = {'FW': 0, 'MID': 1, 'DEF': 2, 'GK': 3}

    def pos_sort(r):
        return POS_ORDER.get((r['position_slot'] or '').upper(), 9)

    starters = sorted([r for r in roster_rows if r['slot_type'] == 'starter'], key=pos_sort)
    bench    = [r for r in roster_rows if r['slot_type'] == 'bench']
    ir       = [r for r in roster_rows if r['slot_type'] == 'ir']

    position_check = check_position_counts(conn, manager_id, current_gw)
    ir_check = check_ir_eligibility(conn, manager_id, current_gw)

    gw_locked  = is_gw_locked(conn, season, current_gw)
    locked_map = {
        r['player_name']: is_player_locked(conn, season, current_gw, club_map.get(r['player_name']))
        for r in roster_rows
    }
    lock_state_map = {
        r['player_name']: get_player_lock_state(conn, season, current_gw, club_map.get(r['player_name']))
        for r in roster_rows
    }
    start_status_map = {
        r['player_name']: r['status']
        for r in c.execute(
            "SELECT player_name, status FROM player_start_status WHERE gw=? AND season=?",
            (current_gw, season)
        ).fetchall()
    }

    # ── Plan Future Lineup: pre-set a later, not-yet-locked gw's lineup ────
    all_gws = [r[0] for r in c.execute(
        "SELECT gw_number FROM gameweeks WHERE season=? ORDER BY gw_number", (season,)
    ).fetchall()]
    future_gws = [g for g in all_gws if g > current_gw and not is_gw_locked(conn, season, g)]

    plan_gw = request.args.get('plan_gw', type=int)
    if plan_gw not in future_gws:
        plan_gw = future_gws[0] if future_gws else None

    plan_starters = plan_bench = plan_ir = []
    plan_position_check = None
    plan_kickoff_map = {}
    plan_opponent_map = {}
    plan_club_map = {}
    plan_locked_map = {}
    plan_gw_locked = False
    if plan_gw:
        plan_roster_rows = c.execute("""
            SELECT r.player_name, r.slot_type, r.position_slot, p.club
            FROM rosters r
            LEFT JOIN players p ON p.name = r.player_name
            WHERE r.manager_id=?
              AND r.gw_start <= ?
              AND (r.gw_end IS NULL OR r.gw_end >= ?)
        """, (manager_id, plan_gw, plan_gw)).fetchall()

        plan_player_names = [r['player_name'] for r in plan_roster_rows]
        plan_eligibility_map = {}
        if plan_player_names:
            ph = ','.join('?' * len(plan_player_names))
            elig_rows = c.execute(f"""
                SELECT p.name, pe.position FROM players p
                JOIN player_eligibility pe ON pe.player_id = p.id
                WHERE p.name IN ({ph})
            """, plan_player_names).fetchall()
            for r in elig_rows:
                plan_eligibility_map.setdefault(r['name'], set()).add(r['position'])
            plan_eligibility_map = {name: sorted(positions) for name, positions in plan_eligibility_map.items()}

        plan_fixture_info = get_gw_fixture_info(conn, season, plan_gw)
        plan_kickoff_map = {club: (info['date'], info['time']) for club, info in plan_fixture_info.items()}
        plan_opponent_map = {club: info['opponent'] for club, info in plan_fixture_info.items()}
        plan_position_check = check_position_counts(conn, manager_id, plan_gw)
        plan_club_map = {r['player_name']: r['club'] for r in plan_roster_rows}
        plan_gw_locked = is_gw_locked(conn, season, plan_gw)
        plan_locked_map = {
            r['player_name']: is_player_locked(conn, season, plan_gw, r['club'])
            for r in plan_roster_rows
        }

        plan_starters = sorted([r for r in plan_roster_rows if r['slot_type'] == 'starter'], key=pos_sort)
        plan_bench    = [r for r in plan_roster_rows if r['slot_type'] == 'bench']
        plan_ir       = [r for r in plan_roster_rows if r['slot_type'] == 'ir']

    # ── Historical Lineup: read-only view of a past gw's roster ────────────
    past_gws = [g for g in all_gws if g < current_gw]

    history_gw = request.args.get('history_gw', type=int)
    if history_gw not in past_gws:
        history_gw = None

    history_starters = history_bench = history_ir = []
    history_matchup = None
    history_top_scorer = None
    if history_gw:
        history_roster = get_roster_at_gw(conn, manager_id, history_gw, season, scoring_season)
        history_starters = sorted([r for r in history_roster if r['slot_type'] == 'starter'],
                                   key=lambda r: POS_ORDER.get(r['real_position'], 9))
        history_bench = [r for r in history_roster if r['slot_type'] == 'bench']
        history_ir    = [r for r in history_roster if r['slot_type'] == 'ir']

        eligible_for_top = [r for r in history_roster if r['slot_type'] in ('starter', 'bench')]
        if eligible_for_top:
            history_top_scorer = max(eligible_for_top, key=lambda r: r['gw_score'])

        history_matchup = c.execute("""
            SELECT
                CASE WHEN mu.team_a_id=? THEN mb.name      ELSE ma.name      END AS opp_name,
                CASE WHEN mu.team_a_id=? THEN mb.team_name ELSE ma.team_name END AS opp_team,
                CASE WHEN mu.team_a_id=? THEN mb.id        ELSE ma.id        END AS opp_id,
                ra.fantasy_score AS my_score,
                rb.fantasy_score AS opp_score,
                ra.win AS my_win, ra.loss AS my_loss, ra.tie AS my_tie
            FROM matchups mu
            JOIN managers ma ON ma.id = mu.team_a_id
            JOIN managers mb ON mb.id = mu.team_b_id
            LEFT JOIN results ra
                ON ra.matchup_id = mu.id AND ra.manager_id = ?
            LEFT JOIN results rb
                ON rb.matchup_id = mu.id AND rb.manager_id != ?
            WHERE mu.season=? AND mu.gw_number=?
              AND (mu.team_a_id=? OR mu.team_b_id=?)
        """, (manager_id,)*3 + (manager_id, manager_id, season, history_gw,
                                 manager_id, manager_id)).fetchone()

    conn.close()

    return render_template('team.html',
        manager=manager,
        all_managers=all_managers,
        current_gw=current_gw,
        matchup=matchup,
        starters=starters,
        bench=bench,
        ir=ir,
        stats_map=stats_map,
        club_map=club_map,
        opponent_map=opponent_map,
        kickoff_map={club: (info['date'], info['time']) for club, info in fixture_info.items()},
        points_map=points_map,
        eligibility_map=eligibility_map,
        all_eligibility_map={**eligibility_map, **(plan_eligibility_map if plan_gw else {})},
        badges=badges,
        season=season,
        scraper_status=read_scraper_status(),
        position_check=position_check,
        ir_check=ir_check,
        gw_locked=gw_locked,
        locked_map=locked_map,
        lock_state_map=lock_state_map,
        start_status_map=start_status_map,
        future_gws=future_gws,
        plan_gw=plan_gw,
        plan_starters=plan_starters,
        plan_bench=plan_bench,
        plan_ir=plan_ir,
        plan_eligibility_map=plan_eligibility_map if plan_gw else {},
        plan_kickoff_map=plan_kickoff_map,
        plan_opponent_map=plan_opponent_map,
        plan_position_check=plan_position_check,
        plan_club_map=plan_club_map,
        plan_locked_map=plan_locked_map,
        plan_gw_locked=plan_gw_locked,
        h2h=h2h,
        past_gws=past_gws,
        history_gw=history_gw,
        history_starters=history_starters,
        history_bench=history_bench,
        history_ir=history_ir,
        history_matchup=history_matchup,
        history_top_scorer=history_top_scorer,
    )


VALID_SCORING_POSITIONS = {'FW', 'MID', 'DEF', 'GK'}


def resolve_scoring_position(position_slot, real_position):
    """The position to score a player at for a given gw. Uses the slot they
    were actually started in when it's a real position code -- a real-life
    DEF started at MID this week scores as MID (no clean sheet / goals-
    conceded credit for a slot they aren't playing) -- falling back to
    their real, static position only when position_slot isn't a position
    at all (bench/ir rows store the literal string 'bench'/'ir' there)."""
    slot = (position_slot or '').upper()
    if slot in VALID_SCORING_POSITIONS:
        return slot
    return (real_position or 'MID').upper()


def get_roster_at_gw(conn, manager_id, gw, season, scoring_season=None):
    """
    Full roster (all slot types) as it stood at gw, with each player's
    scoring position and their score for that gw. Shared by the historical
    lineup view and the standings top-scorer section -- both need "who was
    on this manager's roster at gw N and what did they score," they just do
    different things with the result.

    Scoring position is resolved via resolve_scoring_position() -- the
    slot they were actually started in when it's a real position (so a
    real-life DEF started at MID doesn't get clean-sheet credit for a slot
    they aren't playing), falling back to their real, static position only
    for bench/ir rows (where position_slot is literally the string
    'bench'/'ir', not a position at all).
    """
    c = conn.cursor()
    if scoring_season is None:
        rules_row = c.execute(
            "SELECT COUNT(*) FROM scoring_config WHERE season=?", (season,)
        ).fetchone()
        scoring_season = season if rules_row and rules_row[0] > 0 else "2025-26"

    roster_rows = c.execute("""
        SELECT r.player_name, r.slot_type, r.position_slot, p.club, p.position
        FROM rosters r
        LEFT JOIN players p ON p.name = r.player_name
        WHERE r.manager_id=?
          AND r.gw_start <= ?
          AND (r.gw_end IS NULL OR r.gw_end >= ?)
    """, (manager_id, gw, gw)).fetchall()

    results = []
    for row in roster_rows:
        name = row['player_name']
        scoring_position = resolve_scoring_position(row['position_slot'], row['position'])

        # raw_stats has no season column -- gw_number alone is ambiguous
        # between seasons (e.g. both 2025-26 and 2026-27 have a GW1), so
        # this must resolve match_id through fixtures/gameweeks (which are
        # season-scoped) rather than matching raw_stats.gw_number directly,
        # or it would silently sum stats from the wrong season's same-
        # numbered gw too. Matches the scoping calc_team_score_for_gw and
        # team()'s points_map already use.
        match_rows = c.execute("""
            SELECT rs.match_id FROM raw_stats rs
            JOIN fixtures f ON f.match_id = rs.match_id
            JOIN gameweeks g ON g.id = f.gw_id
            WHERE rs.player_name=? AND g.gw_number=? AND f.season=?
        """, (name, gw, season)).fetchall()

        gw_score = 0.0
        for m in match_rows:
            score, _ = calc_player_score(conn, name, m['match_id'], scoring_position, season=scoring_season)
            gw_score += score

        results.append({
            'player_name': name,
            'slot_type': row['slot_type'],
            'position_slot': row['position_slot'],
            'real_position': scoring_position,
            'club': row['club'],
            'gw_score': round(gw_score, 2),
        })

    return results


@app.route('/matchup/<int:matchup_id>')
def matchup_detail(matchup_id):
    """Head-to-head matchup detail: both rosters side by side, starters
    position-aligned (ESPN-Fantasy-style), bench listed below. Reuses
    get_roster_at_gw() for each side -- already returns the right scoring
    position per player (the slot they were actually started in, thanks to
    resolve_scoring_position), which is exactly what's needed to group
    starters by fantasy position here too."""
    conn = get_db()
    c = conn.cursor()
    season = DRAFT_SEASON
    badges = load_badges()

    mu = c.execute("""
        SELECT mu.id, mu.gw_number, mu.team_a_id, mu.team_b_id,
               ma.name AS a_name, ma.team_name AS a_team, ma.photo_path AS a_photo,
               mb.name AS b_name, mb.team_name AS b_team, mb.photo_path AS b_photo,
               ra.fantasy_score AS a_score, ra.win AS a_win, ra.loss AS a_loss, ra.tie AS a_tie,
               rb.fantasy_score AS b_score, rb.win AS b_win, rb.loss AS b_loss, rb.tie AS b_tie
        FROM matchups mu
        JOIN managers ma ON ma.id = mu.team_a_id
        JOIN managers mb ON mb.id = mu.team_b_id
        LEFT JOIN results ra ON ra.matchup_id = mu.id AND ra.manager_id = mu.team_a_id
        LEFT JOIN results rb ON rb.matchup_id = mu.id AND rb.manager_id = mu.team_b_id
        WHERE mu.id=?
    """, (matchup_id,)).fetchone()
    if not mu:
        conn.close()
        return "Matchup not found", 404

    gw = mu['gw_number']

    live_score_a = live_score_b = None
    gw_has_any_data = c.execute(
        "SELECT 1 FROM raw_stats WHERE gw_number=? AND external=0 LIMIT 1", (gw,)
    ).fetchone() is not None
    if gw_has_any_data:
        if mu['a_score'] is None:
            live_score_a, _ = calc_team_score_for_gw(conn, mu['team_a_id'], gw, season=season)
        if mu['b_score'] is None:
            live_score_b, _ = calc_team_score_for_gw(conn, mu['team_b_id'], gw, season=season)

    # club -> {'opponent', 'score'} for this gw, score as "club's goals-opponent's
    # goals" (None if not played yet) -- get_gw_fixture_info only has the
    # opponent name, not the actual final score, which ESPN's reference
    # view shows next to every player regardless of slot.
    club_match_info = {}
    for f in c.execute("""
        SELECT f.home_club, f.away_club, f.goals_home, f.goals_away
        FROM fixtures f JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number=? AND f.season=?
    """, (gw, season)).fetchall():
        played = f['goals_home'] is not None and f['goals_away'] is not None
        club_match_info[f['home_club']] = {
            'opponent': f['away_club'],
            'score': f"{f['goals_home']}-{f['goals_away']}" if played else None,
        }
        club_match_info[f['away_club']] = {
            'opponent': f['home_club'],
            'score': f"{f['goals_away']}-{f['goals_home']}" if played else None,
        }

    def build_side(manager_id):
        roster = get_roster_at_gw(conn, manager_id, gw, season)
        for r in roster:
            r['match_info'] = club_match_info.get(r['club'])
        return {
            'starters': [r for r in roster if r['slot_type'] == 'starter'],
            'bench': [r for r in roster if r['slot_type'] == 'bench'],
            'ir': [r for r in roster if r['slot_type'] == 'ir'],
        }

    side_a = build_side(mu['team_a_id'])
    side_b = build_side(mu['team_b_id'])

    # One row per fantasy starting slot (2 FW, 4 MID, 4 DEF, 1 GK), pairing
    # both teams' Nth player at that position -- blank if a side is short.
    paired_starters = []
    for pos, target in POSITION_TARGETS.items():
        a_group = [r for r in side_a['starters'] if r['real_position'] == pos]
        b_group = [r for r in side_b['starters'] if r['real_position'] == pos]
        for i in range(target):
            paired_starters.append({
                'pos': pos,
                'a': a_group[i] if i < len(a_group) else None,
                'b': b_group[i] if i < len(b_group) else None,
            })

    conn.close()
    return render_template('matchup.html',
        matchup_id=matchup_id, gw=gw, season=season, badges=badges,
        team_a_id=mu['team_a_id'], team_a_name=mu['a_name'], team_a_team=mu['a_team'], team_a_photo=mu['a_photo'],
        team_b_id=mu['team_b_id'], team_b_name=mu['b_name'], team_b_team=mu['b_team'], team_b_photo=mu['b_photo'],
        score_a=mu['a_score'], score_b=mu['b_score'],
        win_a=mu['a_win'], loss_a=mu['a_loss'], tie_a=mu['a_tie'],
        win_b=mu['b_win'], loss_b=mu['b_loss'], tie_b=mu['b_tie'],
        live_score_a=live_score_a, live_score_b=live_score_b,
        paired_starters=paired_starters,
        bench_a=side_a['bench'], bench_b=side_b['bench'],
        ir_a=side_a['ir'], ir_b=side_b['ir'],
    )


def apply_slot_change(conn, manager_id, player_name, gw, new_slot_type, new_position_slot):
    """
    Move one player's roster row to a new slot for gw, splitting the
    open-ended roster range at `gw` instead of editing the covering row in
    place, so a change made for one gw can't silently rewrite the slot/
    position that was actually live in other gws. Shared by the single-
    player move (update_roster_slot) and the two-player swap
    (execute_slot_swap) below — both need the identical split logic, just
    applied once or twice.

    Returns (ok, error_or_None, old_slot_type, old_position_slot). Does not
    commit — caller commits.
    """
    c = conn.cursor()

    row = c.execute("""
        SELECT id, slot_type, position_slot, gw_start, gw_end FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, player_name, gw, gw)).fetchone()

    if not row:
        return False, f"{player_name} is not on this roster", None, None

    club_row = c.execute("SELECT club FROM players WHERE name=?", (player_name,)).fetchone()
    club = club_row['club'] if club_row else None

    locked, reason = is_change_locked(conn, DRAFT_SEASON, gw, club, row['slot_type'], new_slot_type)
    if locked:
        return False, reason, None, None

    if row['gw_start'] == gw:
        # A row already starts exactly at this gw (either it's always been
        # this gw's row, or a prior edit already split it here) — just
        # update it in place, no need to split further.
        c.execute("""
            UPDATE rosters SET slot_type=?, position_slot=?
            WHERE id=?
        """, (new_slot_type, new_position_slot, row['id']))
    else:
        # Split: close the existing row the gw before this change, then open
        # a fresh row starting at gw carrying the new slot/position forward —
        # inheriting whatever gw_end (NULL or a scheduled future drop) the
        # original row already had, so we don't accidentally extend or
        # truncate the player's roster ownership window.
        c.execute("UPDATE rosters SET gw_end=? WHERE id=?", (gw - 1, row['id']))
        c.execute("""
            INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (manager_id, player_name, new_slot_type, new_position_slot, gw, row['gw_end']))

    return True, None, row['slot_type'], row['position_slot']


@app.route('/api/roster/update', methods=['POST'])
def update_roster_slot():
    """
    Move a player between slots (starter position / bench / IR) for a given
    gameweek — the current one from the main Team page, or a future one from
    the Plan Future Lineup panel. The dropdown UI already restricts options
    to a player's real eligibility, so no server-side eligibility rejection
    is needed here — but locking and the IR/15-cap rules are enforced below.
    """
    data = request.get_json() or {}
    manager_id = current_manager_id()
    player_name = data.get('player_name')
    gw = data.get('gw')
    new_slot_type = data.get('slot_type')
    new_position_slot = data.get('position_slot')

    if not all([player_name, gw, new_slot_type, new_position_slot]):
        return jsonify({"error": "Missing required fields"}), 400
    gw = int(gw)

    conn = get_db()

    current = conn.execute("""
        SELECT slot_type FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, player_name, gw, gw)).fetchone()
    if not current:
        conn.close()
        return jsonify({"error": "Roster row not found for this player/GW"}), 404

    # Roster shape is fixed: exactly 15 non-IR (starter + bench) plus at most
    # 1 IR slot — IR is a single reserved spot, not extra bench space, so
    # both directions of crossing that boundary need checking here.
    if new_slot_type == 'ir' and current['slot_type'] != 'ir':
        existing_ir = conn.execute("""
            SELECT 1 FROM rosters
            WHERE manager_id=? AND slot_type='ir'
              AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (manager_id, gw, gw)).fetchone()
        if existing_ir:
            conn.close()
            return jsonify({"error": "Only one player can be on IR at a time."}), 409
    elif current['slot_type'] == 'ir' and new_slot_type != 'ir':
        if count_active_roster_slots(conn, manager_id, gw) >= PLAYER_PICKS_PER_TEAM:
            conn.close()
            return jsonify({"error": "Your active roster is already full (15) — drop a player before moving this one off IR."}), 409

    ok, err, old_slot_type, old_position_slot = apply_slot_change(
        conn, manager_id, player_name, gw, new_slot_type, new_position_slot)
    if not ok:
        conn.close()
        status = 404 if 'not on this roster' in err else 403
        return jsonify({"error": err}), status

    log_audit(conn, manager_id, 'roster', 'lineup_change',
              f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Moved {player_name}: {old_slot_type} ({old_position_slot}) → {new_slot_type} ({new_position_slot})",
              {"player": player_name, "gw": gw, "from": {"slot_type": old_slot_type, "position_slot": old_position_slot},
               "to": {"slot_type": new_slot_type, "position_slot": new_position_slot}})
    conn.commit()

    counts = conn.execute("""
        SELECT position_slot, COUNT(*) as cnt
        FROM rosters
        WHERE manager_id=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?) AND slot_type='starter'
        GROUP BY position_slot
    """, (manager_id, gw, gw)).fetchall()

    conn.close()

    formation = {r['position_slot']: r['cnt'] for r in counts}
    return jsonify({"status": "ok", "formation": formation})


@app.route('/api/player-start-status/update', methods=['POST'])
def update_player_start_status():
    """
    Manually record whether a player actually started for their real-world
    club in a gameweek -- nothing scrapes this, it's purely manager-entered.
    Global per (player_name, gw, season), not per-manager, since a player
    is only ever on one active roster at a time. Only the roster owner may
    set it, mirroring update_roster_slot's ownership check exactly: derive
    manager_id from the session (never trust the client) and confirm this
    player is actually on their active roster for this gw.

    status is 'starting' | 'not_starting' | null (null clears back to the
    blank/unknown state -- the tri-state cycle goes blank -> starting ->
    not_starting -> blank).
    """
    data = request.get_json() or {}
    manager_id = current_manager_id()
    player_name = data.get('player_name')
    gw = data.get('gw')
    status = data.get('status')

    if not player_name or not gw:
        return jsonify({"error": "Missing required fields"}), 400
    gw = int(gw)
    if status not in (None, 'starting', 'not_starting'):
        return jsonify({"error": "status must be 'starting', 'not_starting', or null"}), 400

    conn = get_db()
    owns_player = conn.execute("""
        SELECT 1 FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, player_name, gw, gw)).fetchone()
    if not owns_player:
        conn.close()
        return jsonify({"error": "That player isn't on your roster for this GW"}), 404

    if status is None:
        conn.execute(
            "DELETE FROM player_start_status WHERE player_name=? AND gw=? AND season=?",
            (player_name, gw, DRAFT_SEASON)
        )
    else:
        conn.execute("""
            INSERT INTO player_start_status (player_name, gw, season, status, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_name, gw, season) DO UPDATE SET
                status=excluded.status, updated_by=excluded.updated_by, updated_at=excluded.updated_at
        """, (player_name, gw, DRAFT_SEASON, status, manager_id, now_eastern_naive().isoformat()))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "player_name": player_name, "gw": gw, "start_status": status})


def execute_slot_swap(conn, manager_id, player_a, player_b, gw):
    """
    Atomically swap two of a manager's own players' roster slots — each
    inherits the other's exact slot_type/position_slot. Since total counts
    are unchanged both before and after, this never needs the IR-singleton
    or 15-man cap checks update_roster_slot enforces — a swap can't create
    an invalid roster shape by construction. This is what makes both "swap
    my IR player back in" (nobody else needs to move to IR first) and
    "who can take this vacated starter slot" (any eligible teammate,
    regardless of their current slot) work as a single action.
    """
    if player_a == player_b:
        return False, {"error": "Can't swap a player with themselves."}

    c = conn.cursor()
    row_a = c.execute("""
        SELECT slot_type, position_slot FROM rosters
        WHERE manager_id=? AND player_name=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
    """, (manager_id, player_a, gw, gw)).fetchone()
    row_b = c.execute("""
        SELECT slot_type, position_slot FROM rosters
        WHERE manager_id=? AND player_name=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
    """, (manager_id, player_b, gw, gw)).fetchone()

    if not row_a:
        return False, {"error": f"{player_a} is not on this roster"}
    if not row_b:
        return False, {"error": f"{player_b} is not on this roster"}

    # Whichever side lands in a starter slot must actually be eligible to
    # play there — bench/IR destinations never require eligibility.
    def eligible_positions(player_name):
        return {r[0] for r in c.execute("""
            SELECT pe.position FROM players p
            JOIN player_eligibility pe ON pe.player_id = p.id
            WHERE p.name = ?
        """, (player_name,)).fetchall()}

    if row_b['slot_type'] == 'starter' and row_b['position_slot'] not in eligible_positions(player_a):
        return False, {"error": f"{player_a} isn't eligible for {row_b['position_slot']}."}
    if row_a['slot_type'] == 'starter' and row_a['position_slot'] not in eligible_positions(player_b):
        return False, {"error": f"{player_b} isn't eligible for {row_a['position_slot']}."}

    ok, err, _, _ = apply_slot_change(conn, manager_id, player_a, gw, row_b['slot_type'], row_b['position_slot'])
    if not ok:
        return False, {"error": err}
    ok, err, _, _ = apply_slot_change(conn, manager_id, player_b, gw, row_a['slot_type'], row_a['position_slot'])
    if not ok:
        return False, {"error": err}

    log_audit(conn, manager_id, 'roster', 'slot_swap',
              f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Swapped {player_a} ({row_a['slot_type']}/{row_a['position_slot']}) "
              f"↔ {player_b} ({row_b['slot_type']}/{row_b['position_slot']})",
              {"player_a": player_a, "player_b": player_b, "gw": gw})
    return True, {"status": "ok"}


@app.route('/api/roster/swap_slots', methods=['POST'])
def swap_roster_slots():
    data = request.get_json() or {}
    manager_id = current_manager_id()
    player_a = data.get('player_a')
    player_b = data.get('player_b')
    gw = data.get('gw')

    if not all([player_a, player_b, gw]):
        return jsonify({"error": "Missing required fields"}), 400
    gw = int(gw)

    conn = get_db()
    ok, result = execute_slot_swap(conn, manager_id, player_a, player_b, gw)
    if not ok:
        conn.close()
        return jsonify(result), 409

    conn.commit()
    conn.close()
    return jsonify(result)


def execute_roster_drop(conn, manager_id, drop_player, gw, source='roster_drop'):
    """
    Release a player from the roster with no replacement — the mirror image
    of execute_roster_swap's add-without-drop path, but for the drop side
    alone. Frees a roster spot (to be filled later via Add/Drop, or left
    open) without requiring an immediate replacement pick.
    """
    c = conn.cursor()

    drop_row = c.execute("""
        SELECT id, slot_type, position_slot FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, drop_player, gw, gw)).fetchone()
    if not drop_row:
        return False, {"error": f"{drop_player} is not on this roster"}

    drop_club_row = c.execute("SELECT club FROM players WHERE name=?", (drop_player,)).fetchone()
    drop_club = drop_club_row['club'] if drop_club_row else None
    locked, reason = is_change_locked(conn, DRAFT_SEASON, gw, drop_club, drop_row['slot_type'], None)
    if locked:
        return False, {"error": reason}

    c.execute("UPDATE rosters SET gw_end=? WHERE id=?", (gw - 1 if gw > 1 else gw, drop_row['id']))
    if gw == 1:
        c.execute("DELETE FROM rosters WHERE id=?", (drop_row['id'],))

    c.execute("""
        INSERT INTO transactions (manager_id, added_player, dropped_player, source, gw, season, created_at)
        VALUES (?, NULL, ?, ?, ?, ?, ?)
    """, (manager_id, drop_player, source, gw, DRAFT_SEASON, now_eastern_naive().isoformat()))

    log_audit(conn, manager_id, 'roster', 'drop_only',
              f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Dropped {drop_player} (no replacement)",
              {"dropped": drop_player, "gw": gw, "source": source})

    return True, {"status": "ok"}


@app.route('/api/roster/drop', methods=['POST'])
def drop_roster_player():
    data = request.get_json() or {}
    manager_id = current_manager_id()
    drop_player = data.get('player_name')
    gw = data.get('gw')

    if not drop_player or not gw:
        return jsonify({"error": "Missing required fields"}), 400
    gw = int(gw)

    conn = get_db()
    ok, result = execute_roster_drop(conn, manager_id, drop_player, gw)
    if not ok:
        conn.close()
        status = 404 if 'not on this roster' in result['error'] else 403
        return jsonify(result), status

    conn.commit()
    conn.close()
    return jsonify(result)


# ── Manager Profile (team name + photo) ─────────────────────────────────────

@app.route('/api/manager/rename', methods=['POST'])
def rename_team():
    data = request.get_json() or {}
    manager_id = current_manager_id()
    team_name = (data.get('team_name') or '').strip()

    if not team_name:
        return jsonify({"error": "Missing team_name"}), 400
    if len(team_name) > 40:
        return jsonify({"error": "Team name must be 40 characters or fewer"}), 400

    conn = get_db()
    row = conn.execute("SELECT id, team_name FROM managers WHERE id=?", (manager_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Manager not found"}), 404

    conn.execute("UPDATE managers SET team_name=? WHERE id=?", (team_name, manager_id))
    log_audit(conn, manager_id, 'manager', 'rename_team',
              f"Renamed team: \"{row['team_name']}\" → \"{team_name}\"")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "team_name": team_name})


@app.route('/api/manager/photo', methods=['POST'])
def upload_manager_photo():
    manager_id = current_manager_id()
    file = request.files.get('photo')

    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_PHOTO_EXTS:
        return jsonify({"error": "Unsupported file type. Use PNG, JPG, GIF, or WEBP."}), 400

    conn = get_db()
    row = conn.execute("SELECT id, photo_path FROM managers WHERE id=?", (manager_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Manager not found"}), 404

    os.makedirs(PHOTOS_DIR, exist_ok=True)

    # Unique filename per upload so browser caches never serve a stale photo.
    filename = secure_filename(f"manager_{manager_id}_{uuid.uuid4().hex[:8]}.{ext}")
    file.save(os.path.join(PHOTOS_DIR, filename))

    old_photo = row['photo_path']
    if old_photo:
        old_path = os.path.join(os.path.dirname(__file__), 'static', old_photo)
        if os.path.exists(old_path):
            os.remove(old_path)

    relative_path = f"uploads/team_photos/{filename}"
    conn.execute("UPDATE managers SET photo_path=? WHERE id=?", (relative_path, manager_id))
    log_audit(conn, manager_id, 'manager', 'change_photo', "Changed team photo")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "photo_path": relative_path})


# ── Player History ───────────────────────────────────────────────────────────

@app.route('/api/manager/<int:manager_id>/roster')
def manager_roster_api(manager_id):
    """Fresh roster for a manager at a given gw (defaults to the current
    one) — used by the Add/Claim drop-player picker (always current gw, so
    it never shows a stale list from an earlier page load) and the Team
    page's swap picker, which passes ?gw= for the Plan Future Lineup panel
    so swap candidates reflect that future gw's planned roster, not today's."""
    conn = get_db()
    season = DRAFT_SEASON
    gw = request.args.get('gw', type=int) or get_current_gw(conn, season)
    rows = conn.execute("""
        SELECT player_name, slot_type, position_slot
        FROM rosters
        WHERE manager_id=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
        ORDER BY slot_type, position_slot
    """, (manager_id, gw, gw)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/history')
def history():
    conn = get_db()
    c    = conn.cursor()
    season = '2026-27'

    current_gw = get_current_gw(conn, season)
    owner_map  = get_owner_map(conn, season, current_gw)

    managers = c.execute(
        "SELECT id, name, team_name FROM managers ORDER BY name"
    ).fetchall()
    # "Managing as" used to be a dropdown anyone could switch — now it's
    # always the logged-in identity (or None if browsing without a session).
    as_manager_id = current_manager_id()

    # compute_full_player_stats's totals_2025/eligibility_by_player are the
    # exact same computation this route used to also run separately (a
    # ~2,500-query calc_bulk_season_totals pass, run twice for nothing) --
    # call it once and reuse everything it returns.
    totals_2025, eligibility_by_player, stat_sums_2025, _projections = compute_full_player_stats(conn)
    totals_2026 = calc_bulk_season_totals(conn, '2026-27', match_id_filter=(SEASON_CUTOFF, 9_999_999))

    # Players with no club (or at a relegated club) aren't in the current PL
    # player pool — exclude from browse, but their historical stats remain.
    browse_rows = c.execute(f"""
        SELECT p.name, p.club, p.position
        FROM players p
        WHERE p.club IS NOT NULL AND p.club != ''
          AND p.club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
              AND p.draftable = 1
        ORDER BY p.name
    """, RELEGATED_CLUBS).fetchall()

    fixture_info = get_gw_fixture_info(conn, season, current_gw)

    browse_players = []
    for p in browse_rows:
        owner = owner_map.get(p['name'])
        eligible = sorted(eligibility_by_player.get(p['name']) or ([p['position']] if p['position'] else []))
        s25 = totals_2025.get(p['name'], {'total': 0.0, 'avg': 0.0})
        s26 = totals_2026.get(p['name'], {'total': 0.0, 'avg': 0.0})
        next_match = fixture_info.get(p['club'])
        browse_players.append({
            'name': p['name'],
            'club': p['club'],
            'eligibility': eligible,
            'owner_manager_id': owner['manager_id'] if owner else None,
            'owner_name': owner['manager_name'] if owner else None,
            'owner_team': owner['team_name'] if owner else None,
            'pts_2025_26': s25['total'],
            'avg_2025_26': s25['avg'],
            'pts_2026_27': s26['total'],
            'avg_2026_27': s26['avg'],
            'next_opponent': next_match['opponent'] if next_match else None,
            'next_kickoff': (next_match['date'], next_match['time']) if next_match and next_match['date'] else None,
            'stats': stat_sums_2025.get(p['name'], {}),
        })

    manager_roster = []
    if as_manager_id:
        roster_rows = c.execute("""
            SELECT player_name, slot_type, position_slot
            FROM rosters
            WHERE manager_id=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
            ORDER BY slot_type, position_slot
        """, (as_manager_id, current_gw, current_gw)).fetchall()
        manager_roster = [dict(r) for r in roster_rows]

    transactions = c.execute("""
        SELECT t.*, m.name AS manager_name, m.team_name
        FROM transactions t
        JOIN managers m ON m.id = t.manager_id
        ORDER BY t.id DESC
        LIMIT 200
    """).fetchall()

    waiver_window = get_open_waiver_window(conn, season)
    waiver_order = get_waiver_order(conn, season)

    my_claims = []
    if as_manager_id and waiver_window:
        my_claims = [dict(r) for r in c.execute("""
            SELECT * FROM waiver_claims
            WHERE manager_id=? AND status='pending' AND window_id=?
            ORDER BY priority
        """, (as_manager_id, waiver_window['id'])).fetchall()]

    my_pending_claims = []
    if as_manager_id:
        my_pending_claims = [dict(r) for r in c.execute("""
            SELECT * FROM pending_waiver_claims WHERE manager_id=? AND season=? ORDER BY id
        """, (as_manager_id, season)).fetchall()]

    waiver_windows_rows = c.execute(
        "SELECT * FROM waiver_windows WHERE season=? AND status='complete' ORDER BY window_number DESC", (season,)
    ).fetchall()
    waiver_results = []
    for w in waiver_windows_rows:
        claims = c.execute("""
            SELECT wc.*, m.name AS manager_name, m.team_name
            FROM waiver_claims wc JOIN managers m ON m.id = wc.manager_id
            WHERE wc.window_id=? AND wc.status != 'pending' ORDER BY wc.sequence_number
        """, (w['id'],)).fetchall()
        waiver_results.append({**dict(w), 'claims': [dict(cl) for cl in claims]})

    my_trades = []
    if as_manager_id:
        my_trades = [dict(t) for t in c.execute("""
            SELECT t.*, p.name AS proposer_name, p.team_name AS proposer_team,
                   tgt.name AS target_name, tgt.team_name AS target_team
            FROM player_trades t
            JOIN managers p ON p.id = t.proposer_manager_id
            JOIN managers tgt ON tgt.id = t.target_manager_id
            WHERE t.season=? AND (t.proposer_manager_id=? OR t.target_manager_id=?)
            ORDER BY t.created_at DESC
        """, (season, as_manager_id, as_manager_id)).fetchall()]
        trade_items_by_trade = {}
        for it in c.execute("""
            SELECT * FROM player_trade_items
            WHERE trade_id IN (
                SELECT id FROM player_trades
                WHERE season=? AND (proposer_manager_id=? OR target_manager_id=?)
            )
        """, (season, as_manager_id, as_manager_id)).fetchall():
            trade_items_by_trade.setdefault(it['trade_id'], []).append(dict(it))
        for t in my_trades:
            t['give_items'] = [i for i in trade_items_by_trade.get(t['id'], []) if i['from_manager_id'] == t['proposer_manager_id']]
            t['receive_items'] = [i for i in trade_items_by_trade.get(t['id'], []) if i['from_manager_id'] == t['target_manager_id']]

    my_shortlist = get_my_shortlist(conn)
    conn.close()
    return render_template('history.html',
        season=season,
        my_trades=my_trades,
        scraper_status=read_scraper_status(),
        browse_players=browse_players,
        managers=managers,
        as_manager_id=as_manager_id,
        current_gw=current_gw,
        manager_roster=manager_roster,
        transactions=transactions,
        my_shortlist=my_shortlist,
        waiver_window=dict(waiver_window) if waiver_window else None,
        waiver_order=waiver_order,
        my_claims=my_claims,
        my_pending_claims=my_pending_claims,
        waiver_results=waiver_results,
        stat_cols=STAT_COLS,
    )


@app.route('/api/player_history')
def player_history():
    name = request.args.get('name', '')
    if not name:
        return jsonify({"error": "Missing name"}), 400

    conn = get_db()
    c = conn.cursor()

    pos_row = c.execute("SELECT position FROM players WHERE name=?", (name,)).fetchone()
    pos = (pos_row['position'] if pos_row else 'MID').upper()

    fixture_rows = c.execute("""
        SELECT g.gw_number, f.home_club, f.away_club, f.season
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
    """).fetchall()
    opp_lookup = {}
    for row in fixture_rows:
        opp_lookup[(row['home_club'], row['gw_number'], row['season'])] = row['away_club']
        opp_lookup[(row['away_club'], row['gw_number'], row['season'])] = row['home_club']

    rows = c.execute("""
        SELECT match_id, gw_number, club, goals, assists, shots_on_target, key_passes,
               dribbles, tackles, interceptions, clearances, blocked_shots,
               acc_crosses, acc_long_balls, saves, pk_saves, glc, lmt, elg,
               own_goals, motm, yellow_cards, red_cards, minutes_played
        FROM raw_stats
        WHERE player_name=?
        ORDER BY match_id
    """, (name,)).fetchall()

    results = []
    for r in rows:
        row_season = '2026-27' if r['match_id'] >= SEASON_CUTOFF else '2025-26'
        scoring_season = row_season
        rules_row = c.execute(
            "SELECT COUNT(*) FROM scoring_config WHERE season=?", (scoring_season,)
        ).fetchone()
        if not rules_row or rules_row[0] == 0:
            scoring_season = '2025-26'
        fpts, _ = calc_player_score(conn, name, r['match_id'], pos, season=scoring_season)
        row = dict(r)
        row['season'] = row_season
        row['opponent'] = opp_lookup.get((r['club'], r['gw_number'], row_season))
        row['fpts'] = fpts

        # goals_conceded / clean_sheet are derived from the match scoreline,
        # not stored per-player — computed the same way scoring_engine does
        # it internally so the display always matches what was scored.
        if pos in ('DEF', 'GK'):
            gc = get_team_goals_conceded(conn, r['match_id'], r['club'])
            row['goals_conceded'] = gc
            minutes = r['minutes_played'] or 0
            row['clean_sheet'] = 1 if (minutes >= 60 and gc == 0) else 0
        else:
            row['goals_conceded'] = None
            row['clean_sheet'] = None

        results.append(row)

    proj_row = c.execute(
        "SELECT proj_total, proj_avg FROM player_projections WHERE season=? AND player_name=?",
        ('2026-27', name)
    ).fetchone()

    conn.close()
    results.sort(key=lambda r: (r['season'], r['gw_number']))
    return jsonify({
        "history": results,
        "projection": dict(proj_row) if proj_row else None,
    })


def count_active_roster_slots(conn, manager_id, gw):
    """Count of a manager's non-IR (starter + bench) roster spots active at
    gw. IR doesn't count against the 15-man cap — moving someone to a
    previously-empty IR slot effectively opens a 16th spot to add into
    without needing to drop anyone."""
    row = conn.execute("""
        SELECT COUNT(*) AS cnt FROM rosters
        WHERE manager_id=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
          AND slot_type IN ('starter', 'bench')
    """, (manager_id, gw, gw)).fetchone()
    return row['cnt']


def execute_roster_swap(conn, manager_id, add_player, drop_player, gw, source, to_ir=False):
    """
    Core roster swap: drop `drop_player` from manager_id's roster and add
    `add_player` in their place. If the incoming player is eligible for the
    outgoing player's exact slot, they inherit it; otherwise they land on the
    bench. Follows the open-ended roster range model (gw_start=gw, gw_end=NULL).

    `drop_player` may be None/falsy — a pure add, only allowed when the
    manager has an open non-IR roster spot (see count_active_roster_slots).
    `to_ir` is an explicit, always-honored request to land the incoming
    player on IR instead of the bench — IR is a single reserved spot that
    doesn't count against the 15-man cap (see count_active_roster_slots's
    docstring). It's safe whether or not a drop_player is also given: if
    the dropped player is themself the current IR occupant, this drop
    frees the exact slot being filled; otherwise IR must currently be
    empty, which is checked either way.

    Shared by the instant Add/Drop endpoint and the waiver processing
    algorithm — `source` tags the resulting transaction ('roster_swap' or
    'waiver_claim'). Returns (True, {slot_type, position_slot}) on success or
    (False, {"error": "..."}) on failure. Does not commit — caller commits.
    """
    c = conn.cursor()

    drop_row = None
    if drop_player:
        drop_row = c.execute("""
            SELECT id, slot_type, position_slot FROM rosters
            WHERE manager_id=? AND player_name=?
              AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
        """, (manager_id, drop_player, gw, gw)).fetchone()

        if not drop_row:
            return False, {"error": f"{drop_player} is not on this roster"}

        # A dropped player leaves the roster entirely — pass new_slot_type=None
        # (not becoming a starter) so is_change_locked only blocks this when
        # drop_row['slot_type'] is itself 'starter' (a locked starter can't be
        # dropped); bench/IR players remain droppable regardless of their own
        # kickoff, per the shared lock rule.
        drop_club_row = c.execute("SELECT club FROM players WHERE name=?", (drop_player,)).fetchone()
        drop_club = drop_club_row['club'] if drop_club_row else None
        locked, reason = is_change_locked(conn, DRAFT_SEASON, gw, drop_club, drop_row['slot_type'], None)
        if locked:
            return False, {"error": reason}
        if to_ir and drop_row['slot_type'] != 'ir':
            # Sending the incoming player to IR, but this drop isn't the
            # one vacating it — IR must already be empty independently.
            existing_ir = c.execute("""
                SELECT 1 FROM rosters
                WHERE manager_id=? AND slot_type='ir'
                  AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
            """, (manager_id, gw, gw)).fetchone()
            if existing_ir:
                return False, {"error": "Only one player can be on IR at a time."}
    elif to_ir:
        existing_ir = c.execute("""
            SELECT 1 FROM rosters
            WHERE manager_id=? AND slot_type='ir'
              AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (manager_id, gw, gw)).fetchone()
        if existing_ir:
            return False, {"error": "Only one player can be on IR at a time."}
    else:
        if count_active_roster_slots(conn, manager_id, gw) >= PLAYER_PICKS_PER_TEAM:
            return False, {"error": "Your roster is full — drop a player to add someone new (or free up a spot by moving someone to IR)."}

    owned = c.execute("""
        SELECT 1 FROM rosters
        WHERE player_name=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (add_player, gw, gw)).fetchone()
    if owned:
        return False, {"error": f"{add_player} is already owned"}

    eligible = {r[0] for r in c.execute("""
        SELECT pe.position FROM players p
        JOIN player_eligibility pe ON pe.player_id = p.id
        WHERE p.name = ?
    """, (add_player,)).fetchall()}

    if to_ir:
        # Matches the literal 'ir'/'ir' convention update_roster_slot() uses
        # (team.html's slot <select> sends value="ir|ir") — IR isn't a real
        # position, so position_slot is the marker string, not a position code.
        # Explicit intent always wins here, even over an eligible dropped slot.
        new_slot_type = 'ir'
        new_position_slot = 'ir'
    elif drop_row and drop_row['position_slot'] in eligible:
        new_slot_type = drop_row['slot_type']
        new_position_slot = drop_row['position_slot']
    else:
        new_slot_type = 'bench'
        new_position_slot = sorted(eligible)[0] if eligible else None

    if drop_row:
        # Close out the dropped player's open roster row.
        c.execute("UPDATE rosters SET gw_end=? WHERE id=?", (gw - 1 if gw > 1 else gw, drop_row['id']))
        if gw == 1:
            # Nothing to close before GW1 — just delete the row instead of a zero-length range.
            c.execute("DELETE FROM rosters WHERE id=?", (drop_row['id'],))

    c.execute("""
        INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, (manager_id, add_player, new_slot_type, new_position_slot, gw))

    landed_on_ir = new_slot_type == 'ir'
    c.execute("""
        INSERT INTO transactions (manager_id, added_player, dropped_player, source, gw, season, created_at, to_ir)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (manager_id, add_player, drop_player, source, gw, DRAFT_SEASON, now_eastern_naive().isoformat(),
          1 if landed_on_ir else 0))

    # Transfer-draft picks log their own richer entry (round/draft type) at
    # the call site, since a pass there never reaches this function at all —
    # skip here to avoid a duplicate, less-detailed entry for the same pick.
    if not source.endswith('_transfer_draft'):
        action = 'waiver_claim' if source == 'waiver_claim' else 'add_drop'
        summary = f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Added {add_player}"
        if drop_player:
            summary += f", dropped {drop_player}"
        elif landed_on_ir:
            summary += " — placed directly on IR (no drop needed)"
        else:
            summary += " (no drop — had an open roster spot)"
        log_audit(conn, manager_id, 'roster', action, summary,
                  {"added": add_player, "dropped": drop_player, "gw": gw, "source": source, "to_ir": landed_on_ir})

    return True, {"slot_type": new_slot_type, "position_slot": new_position_slot}


@app.route('/api/roster/swap', methods=['POST'])
def swap_roster_player():
    data = request.get_json() or {}
    manager_id  = current_manager_id()
    drop_player = data.get('drop_player') or None
    add_player  = data.get('add_player')
    to_ir       = bool(data.get('to_ir'))
    gw          = data.get('gw')

    if not add_player or not gw:
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()

    # An instant add of a player whose real club has already kicked off
    # would let anyone snipe a known-good performance ahead of the fair
    # waiver queue -- redirect it into a claim instead, regardless of
    # whether a window happens to be open right now (see
    # submit_or_queue_claim's docstring for why we never auto-open one).
    add_club_row = conn.execute("SELECT club FROM players WHERE name=?", (add_player,)).fetchone()
    add_club = add_club_row['club'] if add_club_row else None
    if add_club and is_player_locked(conn, DRAFT_SEASON, gw, add_club):
        ok, err = validate_claim_target(conn, manager_id, add_player, drop_player, gw, to_ir)
        if not ok:
            conn.close()
            status = 404 if 'not on this roster' in err else 409
            return jsonify({"error": err}), status

        result = submit_or_queue_claim(conn, manager_id, add_player, drop_player, gw, to_ir, DRAFT_SEASON)
        summary = f"Auto-converted locked add to waiver claim: add {add_player}"
        summary += f", drop {drop_player}" if drop_player else (" — direct to IR" if to_ir else " (no drop needed)")
        log_audit(conn, manager_id, 'waiver',
                  'claim_submitted' if result['window_open'] else 'claim_queued_pending', summary,
                  {"add_player": add_player, "drop_player": drop_player, "gw": gw, "to_ir": to_ir})
        conn.commit()
        conn.close()

        message = (f"{add_player} has already played — submitted as a waiver claim into the open window instead of an instant add."
                   if result['window_open'] else
                   f"{add_player} has already played this gameweek — queued as a waiver claim for the next window.")
        return jsonify({"status": "converted_to_pending_waiver", "message": message, "window_open": result['window_open']})

    ok, info = execute_roster_swap(conn, manager_id, add_player, drop_player, gw, 'roster_swap', to_ir=to_ir)
    if not ok:
        conn.close()
        status = 404 if 'not on this roster' in info['error'] else 409
        return jsonify(info), status

    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok",
        "dropped": drop_player,
        "added": add_player,
        "slot_type": info['slot_type'],
        "position_slot": info['position_slot'],
    })


# ── In-season player trades ─────────────────────────────────────────────────
# Trade rostered players between two managers — propose (N-for-M), the other
# manager accepts or declines. Mirrors the Main Draft pick-trade feature
# (player_trades/player_trade_items parallel draft_pick_trades/items) but for
# real roster players at any point during the season, not just pre-draft.

def execute_player_trade(conn, trade_id, acting_manager_id):
    """
    Applies an accepted player trade. Re-validates every item's current
    ownership and lock state (guards the propose->accept race — a player
    may have been dropped or traded away in the meantime), then checks the
    15-man cap for both managers, and only mutates rosters if everything
    passes (all-or-nothing). Every incoming player always lands on the
    bench (no natural 1:1 slot mapping for an N-for-M trade), so no
    eligibility check is needed — bench never requires it.

    Returns (True, None) or (False, {"error": "..."}). Does not commit —
    caller commits.
    """
    c = conn.cursor()
    gw = get_current_gw(conn, DRAFT_SEASON)

    # Checked unconditionally, not just per-item, since a one-sided trade
    # (one side gives nothing) would otherwise skip every per-item lock
    # check on that side and let a fully-locked gw's trade through.
    if is_gw_locked(conn, DRAFT_SEASON, gw):
        return False, {"error": f"GW{gw} is locked — this trade can no longer be applied."}

    items = c.execute("SELECT * FROM player_trade_items WHERE trade_id=?", (trade_id,)).fetchall()

    rows_by_item = {}
    for it in items:
        row = c.execute("""
            SELECT id, slot_type, position_slot FROM rosters
            WHERE manager_id=? AND player_name=?
              AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (it['from_manager_id'], it['player_name'], gw, gw)).fetchone()
        if not row:
            return False, {"error": f"{it['player_name']} has changed hands since this trade was proposed."}
        rows_by_item[it['id']] = row

        if row['slot_type'] == 'starter':
            club_row = c.execute("SELECT club FROM players WHERE name=?", (it['player_name'],)).fetchone()
            club = club_row['club'] if club_row else None
            locked, reason = is_change_locked(conn, DRAFT_SEASON, gw, club, 'starter', None)
            if locked:
                return False, {"error": reason}

    for manager_id in {mid for it in items for mid in (it['from_manager_id'], it['to_manager_id'])}:
        outgoing_active = sum(
            1 for it in items
            if it['from_manager_id'] == manager_id and rows_by_item[it['id']]['slot_type'] in ('starter', 'bench')
        )
        incoming = sum(1 for it in items if it['to_manager_id'] == manager_id)
        new_active = count_active_roster_slots(conn, manager_id, gw) - outgoing_active + incoming
        if new_active > 15:
            manager_name = c.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()['name']
            return False, {"error": f"{manager_name}'s roster would exceed the 15-player limit."}

    now = now_eastern_naive().isoformat()
    for it in items:
        row = rows_by_item[it['id']]
        eligible = {r[0] for r in c.execute("""
            SELECT pe.position FROM players p
            JOIN player_eligibility pe ON pe.player_id = p.id
            WHERE p.name = ?
        """, (it['player_name'],)).fetchall()}

        c.execute("UPDATE rosters SET gw_end=? WHERE id=?", (gw - 1 if gw > 1 else gw, row['id']))
        if gw == 1:
            c.execute("DELETE FROM rosters WHERE id=?", (row['id'],))

        c.execute("""
            INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
            VALUES (?, ?, 'bench', ?, ?, NULL)
        """, (it['to_manager_id'], it['player_name'], sorted(eligible)[0] if eligible else None, gw))

        c.execute("""
            INSERT INTO transactions (manager_id, added_player, dropped_player, source, gw, season, created_at)
            VALUES (?, NULL, ?, 'player_trade', ?, ?, ?)
        """, (it['from_manager_id'], it['player_name'], gw, DRAFT_SEASON, now))
        c.execute("""
            INSERT INTO transactions (manager_id, added_player, dropped_player, source, gw, season, created_at)
            VALUES (?, ?, NULL, 'player_trade', ?, ?, ?)
        """, (it['to_manager_id'], it['player_name'], gw, DRAFT_SEASON, now))

    trade = c.execute("SELECT * FROM player_trades WHERE id=?", (trade_id,)).fetchone()
    proposer = c.execute("SELECT name FROM managers WHERE id=?", (trade['proposer_manager_id'],)).fetchone()['name']
    target = c.execute("SELECT name FROM managers WHERE id=?", (trade['target_manager_id'],)).fetchone()['name']
    give = [it['player_name'] for it in items if it['from_manager_id'] == trade['proposer_manager_id']]
    receive = [it['player_name'] for it in items if it['from_manager_id'] == trade['target_manager_id']]
    log_audit(conn, acting_manager_id, 'player_trade', 'accept',
              f"Trade accepted: {proposer} gave {', '.join(give) or 'nothing'} to {target} for {', '.join(receive) or 'nothing'}",
              {"trade_id": trade_id, "give": give, "receive": receive})

    return True, None


def _get_pending_player_trade(conn, trade_id):
    return conn.execute("SELECT * FROM player_trades WHERE id=? AND status='pending'", (trade_id,)).fetchone()


@app.route('/api/trade/propose', methods=['POST'])
def player_trade_propose():
    manager_id = current_manager_id()
    data = request.get_json() or {}
    try:
        target_manager_id = int(data.get('target_manager_id'))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing or invalid target_manager_id."}), 400
    give_players = data.get('give_players') or []
    receive_players = data.get('receive_players') or []

    if target_manager_id == int(manager_id):
        return jsonify({"error": "You can't trade with yourself."}), 400
    if not give_players and not receive_players:
        return jsonify({"error": "Select at least one player to trade."}), 400
    combined = give_players + receive_players
    if len(combined) != len(set(combined)):
        return jsonify({"error": "The same player can't appear on both sides of a trade."}), 400

    conn = get_db()
    target = conn.execute("SELECT id, name FROM managers WHERE id=?", (target_manager_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "Unknown manager."}), 400

    gw = get_current_gw(conn, DRAFT_SEASON)
    items = []
    for player_name in give_players:
        owner = conn.execute("""
            SELECT manager_id FROM rosters
            WHERE player_name=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (player_name, gw, gw)).fetchone()
        if not owner or owner['manager_id'] != int(manager_id):
            conn.close()
            return jsonify({"error": f"You don't currently own {player_name}."}), 409
        items.append((player_name, int(manager_id), target_manager_id))
    for player_name in receive_players:
        owner = conn.execute("""
            SELECT manager_id FROM rosters
            WHERE player_name=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (player_name, gw, gw)).fetchone()
        if not owner or owner['manager_id'] != target_manager_id:
            conn.close()
            return jsonify({"error": f"{target['name']} doesn't currently own {player_name}."}), 409
        items.append((player_name, target_manager_id, int(manager_id)))

    now = now_eastern_naive().isoformat()
    cur = conn.execute(
        "INSERT INTO player_trades (season, proposer_manager_id, target_manager_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (DRAFT_SEASON, manager_id, target_manager_id, now)
    )
    trade_id = cur.lastrowid
    for player_name, from_id, to_id in items:
        conn.execute(
            "INSERT INTO player_trade_items (trade_id, player_name, from_manager_id, to_manager_id) VALUES (?, ?, ?, ?)",
            (trade_id, player_name, from_id, to_id)
        )

    log_audit(conn, manager_id, 'player_trade', 'propose', f"Proposed a trade with {target['name']}",
              {"trade_id": trade_id, "give_players": give_players, "receive_players": receive_players})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "trade_id": trade_id})


@app.route('/api/trade/<int:trade_id>/accept', methods=['POST'])
def player_trade_accept(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_player_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['target_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager this trade was sent to can accept it."}), 403

    ok, info = execute_player_trade(conn, trade_id, manager_id)
    if not ok:
        conn.close()
        return jsonify(info), 409

    conn.execute(
        "UPDATE player_trades SET status='accepted', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/trade/<int:trade_id>/decline', methods=['POST'])
def player_trade_decline(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_player_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['target_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager this trade was sent to can decline it."}), 403

    conn.execute(
        "UPDATE player_trades SET status='declined', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    log_audit(conn, manager_id, 'player_trade', 'decline', f"Declined trade #{trade_id}", {"trade_id": trade_id})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/trade/<int:trade_id>/cancel', methods=['POST'])
def player_trade_cancel(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_player_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['proposer_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager who proposed this trade can cancel it."}), 403

    conn.execute(
        "UPDATE player_trades SET status='cancelled', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    log_audit(conn, manager_id, 'player_trade', 'cancel', f"Cancelled trade #{trade_id}", {"trade_id": trade_id})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Draft ────────────────────────────────────────────────────────────────────

DRAFT_SEASON = '2026-27'
DRAFT_ROUNDS = 16  # 15 player picks + 1 mandatory Summer Transfer token per manager
DRAFT_TEAMS = 8
DRAFT_TOTAL_PICKS = DRAFT_ROUNDS * DRAFT_TEAMS
PLAYER_PICKS_PER_TEAM = 15
POSITION_TARGETS = {'FW': 2, 'MID': 4, 'DEF': 4, 'GK': 1}


def check_position_counts(conn, manager_id, gw):
    """Starter counts per position vs POSITION_TARGETS for a manager's
    lineup in a given gw. Mirrors the client-side check in team.html's
    updateFormationSummary() so the warning is visible on page load too,
    not just after a JS-driven edit. Returns
    {'positions': {'FW': {'count':2,'target':2}, ...}, 'ok': bool} — 'ok' is
    False if ANY position is short OR over its target."""
    rows = conn.execute("""
        SELECT position_slot, COUNT(*) as cnt
        FROM rosters
        WHERE manager_id=? AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?) AND slot_type='starter'
        GROUP BY position_slot
    """, (manager_id, gw, gw)).fetchall()
    counts = {r['position_slot']: r['cnt'] for r in rows}
    positions = {}
    ok = True
    for pos, target in POSITION_TARGETS.items():
        count = counts.get(pos, 0)
        positions[pos] = {'count': count, 'target': target}
        if count != target:
            ok = False
    return {'positions': positions, 'ok': ok}


def check_ir_eligibility(conn, manager_id, gw, season=DRAFT_SEASON):
    """False if the manager's current IR occupant appeared in a raw_stats
    row for gw-1 (i.e. was in their club's live matchday squad, including an
    unused healthy scratch) -- meaning they weren't actually unavailable and
    shouldn't have been parked on IR. Mirrors check_position_counts's shape.
    Returns {'ok': bool, 'player_name': str|None, 'club': str|None}."""
    if gw <= 1:
        return {'ok': True, 'player_name': None, 'club': None}
    row = conn.execute("""
        SELECT r.player_name, p.club
        FROM rosters r
        LEFT JOIN players p ON p.name = r.player_name
        WHERE r.manager_id=? AND r.slot_type='ir'
          AND r.gw_start<=? AND (r.gw_end IS NULL OR r.gw_end>=?)
    """, (manager_id, gw, gw)).fetchone()
    if not row:
        return {'ok': True, 'player_name': None, 'club': None}
    # raw_stats has no season column -- gw_number alone is ambiguous between
    # seasons (e.g. both 2025-26 and 2026-27 have a GW1), so this must
    # resolve match_id through fixtures/gameweeks (season-scoped) rather
    # than matching raw_stats.gw_number directly, same fix as
    # get_roster_at_gw's identical bug earlier this season.
    appeared = conn.execute("""
        SELECT 1 FROM raw_stats rs
        JOIN fixtures f ON f.match_id = rs.match_id
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE rs.player_name=? AND g.gw_number=? AND f.season=? AND rs.external=0
        LIMIT 1
    """, (row['player_name'], gw - 1, season)).fetchone()
    return {'ok': appeared is None, 'player_name': row['player_name'], 'club': row['club']}


def gw_fully_scraped(conn, season, gw_number):
    """True only if every fixture in this gw has at least one raw_stats row
    — i.e. the whole gameweek has actually been played and captured, not
    just some of it. Deliberately independent of the scraper's own exit
    code (which happens to already refuse to report "complete" for a
    partially-played gw) so finalize_gw_results' safety doesn't silently
    depend on that unrelated behavior staying the same."""
    row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM raw_stats rs WHERE rs.match_id = f.match_id
               ) THEN 1 ELSE 0 END) AS scraped
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.season=? AND g.gw_number=?
    """, (season, gw_number)).fetchone()
    return row['total'] > 0 and row['total'] == row['scraped']


def finalize_gw_results(conn, gw_number, season=DRAFT_SEASON):
    """
    Compute and persist final results for every matchup in a gameweek. A
    manager whose starter lineup fails check_position_counts is forced to 0
    for the gw regardless of what they actually earned — every player is
    individually locked by the time the gw's last match kicks off, so the
    formation check gives the same answer here (post-scrape) as it would
    have at that exact instant. Idempotent: safe to call again for the same
    gw (e.g. after a manual rescrape days later) — overwrites prior rows
    rather than erroring or duplicating.

    No-op if the gameweek isn't fully scraped yet (see gw_fully_scraped) —
    win/loss/tie must never be decided off a partial gw, e.g. someone
    pressing the scrape button mid-week while games are still to be played.
    """
    if not gw_fully_scraped(conn, season, gw_number):
        return

    gw_row = conn.execute(
        "SELECT id FROM gameweeks WHERE season=? AND gw_number=?", (season, gw_number)
    ).fetchone()
    if not gw_row:
        return
    gw_id = gw_row['id']

    matchups = conn.execute(
        "SELECT id, team_a_id, team_b_id FROM matchups WHERE season=? AND gw_number=?",
        (season, gw_number)
    ).fetchall()

    for mu in matchups:
        scores = {}
        for manager_id in (mu['team_a_id'], mu['team_b_id']):
            raw_score, _ = calc_team_score_for_gw(conn, manager_id, gw_number, season=season)
            formation_ok = check_position_counts(conn, manager_id, gw_number)['ok']
            ir_ok = check_ir_eligibility(conn, manager_id, gw_number)['ok']
            final_score = raw_score if (formation_ok and ir_ok) else 0.0
            if not formation_ok:
                log_audit(
                    conn, manager_id, 'scoring', 'invalid_lineup_penalty',
                    f"GW{gw_number}: lineup wasn't formation-valid at last kickoff — "
                    f"score forced to 0 (would have been {round(raw_score, 2)})",
                    {"gw_number": gw_number, "season": season, "raw_score": raw_score}
                )
            if not ir_ok:
                log_audit(
                    conn, manager_id, 'scoring', 'ir_violation_penalty',
                    f"GW{gw_number}: IR occupant was in their club's squad the prior gameweek — "
                    f"score forced to 0 (would have been {round(raw_score, 2)})",
                    {"gw_number": gw_number, "season": season, "raw_score": raw_score}
                )
            scores[manager_id] = final_score

        score_a = scores[mu['team_a_id']]
        score_b = scores[mu['team_b_id']]
        if score_a > score_b:
            outcome_a, outcome_b = (1, 0, 0), (0, 1, 0)
        elif score_b > score_a:
            outcome_a, outcome_b = (0, 1, 0), (1, 0, 0)
        else:
            outcome_a, outcome_b = (0, 0, 1), (0, 0, 1)

        for manager_id, score, (win, loss, tie) in (
            (mu['team_a_id'], score_a, outcome_a),
            (mu['team_b_id'], score_b, outcome_b),
        ):
            conn.execute("DELETE FROM results WHERE gw_id=? AND manager_id=?", (gw_id, manager_id))
            conn.execute("""
                INSERT INTO results (gw_id, manager_id, matchup_id, fantasy_score, win, loss, tie)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (gw_id, manager_id, mu['id'], score, win, loss, tie))

    conn.commit()


def get_gw_top_scorer(conn, season, gw_number):
    """Single highest-scoring player across every manager's full roster
    (starters, bench, IR) for this gw -- the league-wide "player of the
    week". Meant to be called once the gw is finalized (see
    finalize_gw_results); returns None if nobody has a roster yet."""
    best = None
    for m in conn.execute("SELECT id, name FROM managers").fetchall():
        for r in get_roster_at_gw(conn, m['id'], gw_number, season):
            if best is None or r['gw_score'] > best['gw_score']:
                best = {
                    'player_name': r['player_name'],
                    'club': r['club'],
                    'gw_score': r['gw_score'],
                    'manager_name': m['name'],
                }
    return best


@app.route('/api/gameweek/close', methods=['POST'])
def close_gameweek():
    """
    Manually trigger finalize_gw_results() for a gameweek -- the button
    counterpart to what the old subprocess-based scraper already did
    automatically on completion. Needed because the local
    scrape-and-upload stopgap (/api/scrape/upload, in use since WhoScored
    started blocking Render's IP) only writes raw_stats and never calls
    finalize_gw_results itself. Safe to call again on an already-closed gw
    (e.g. after a corrective rescrape) -- finalize_gw_results is idempotent.
    """
    data = request.get_json() or {}
    try:
        gw = int(data.get('gw'))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing or invalid gw"}), 400

    conn = get_db()
    season = DRAFT_SEASON
    if not gw_fully_scraped(conn, season, gw):
        conn.close()
        return jsonify({"error": f"GW{gw} isn't fully scraped yet -- not every fixture has stats."}), 409

    finalize_gw_results(conn, gw, season=season)

    matchups = conn.execute("""
        SELECT mu.id, ma.name AS team_a_name, mb.name AS team_b_name,
               ra.fantasy_score AS score_a, rb.fantasy_score AS score_b,
               ra.win AS win_a, rb.win AS win_b, ra.tie AS tie
        FROM matchups mu
        JOIN managers ma ON ma.id = mu.team_a_id
        JOIN managers mb ON mb.id = mu.team_b_id
        JOIN results ra ON ra.matchup_id = mu.id AND ra.manager_id = mu.team_a_id
        JOIN results rb ON rb.matchup_id = mu.id AND rb.manager_id = mu.team_b_id
        WHERE mu.season=? AND mu.gw_number=?
        ORDER BY ma.name
    """, (season, gw)).fetchall()
    summary = [{
        'team_a_name': m['team_a_name'], 'team_b_name': m['team_b_name'],
        'score_a': m['score_a'], 'score_b': m['score_b'],
        'winner': m['team_a_name'] if m['win_a'] else (m['team_b_name'] if m['win_b'] else None),
        'tie': bool(m['tie']),
    } for m in matchups]
    top_scorer = get_gw_top_scorer(conn, season, gw)

    log_audit(conn, current_manager_id(), 'gameweek', 'closed',
              f"Closed GW{gw} — {len(summary)} matchup(s) finalized",
              {"gw": gw, "season": season, "matchups": summary})
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "gw": gw, "matchups": summary, "top_scorer": top_scorer})


# Single source of truth for the 21 raw-stat columns shown on the Main Draft
# pool table and (via compute_full_player_stats) the Transfer Draft tables.
STAT_COLS = [
    ('goals', 'G', 'Goals'), ('assists', 'A', 'Assists'),
    ('shots_on_target', 'SOT', 'Shots on Target'), ('key_passes', 'KP', 'Key Passes'),
    ('dribbles', 'Drb', 'Dribbles'), ('tackles', 'Tkl', 'Tackles'),
    ('interceptions', 'Int', 'Interceptions'), ('clearances', 'Clr', 'Clearances'),
    ('blocked_shots', 'BlkSh', 'Blocked Shots'), ('acc_crosses', 'Crs', 'Accurate Crosses'),
    ('acc_long_balls', 'LB', 'Accurate Long Balls'), ('saves', 'Saves', 'Saves'),
    ('pk_saves', 'PKSv', 'Penalty Saves'), ('glc', 'GLC', 'Goal Line Clearance'),
    ('lmt', 'LMT', 'Last Man Tackle'), ('elg', 'ELG', 'Error Leading to Goal'),
    ('own_goals', 'OG', 'Own Goals'), ('motm', 'MOTM', 'Man of the Match'),
    ('yellow_cards', 'YC', 'Yellow Cards'), ('red_cards', 'RC', 'Red Cards'),
    ('minutes_played', 'Mins', 'Minutes Played'),
]


# ── Waiver Wire ──────────────────────────────────────────────────────────────

def get_open_waiver_window(conn, season):
    return conn.execute(
        "SELECT * FROM waiver_windows WHERE season=? AND status='open'", (season,)
    ).fetchone()


def validate_claim_target(conn, manager_id, add_player, drop_player, gw, to_ir):
    """Best-effort pre-flight check shared by waiver_claim(), the locked-add
    redirect in swap_roster_player(), and pending-claim promotion in
    waiver_open() -- ownership, drop-row existence, roster capacity, and IR
    occupancy. This is deliberately "best effort": real enforcement happens
    again inside execute_roster_swap at actual processing time, since
    roster state can drift between submitting a claim and it being applied.
    Returns (ok: bool, error: str-or-None).
    """
    c = conn.cursor()
    owned = c.execute("""
        SELECT 1 FROM rosters
        WHERE player_name=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (add_player, gw, gw)).fetchone()
    if owned:
        return False, f"{add_player} is already owned"

    drop_row = None
    if drop_player:
        drop_row = c.execute("""
            SELECT slot_type FROM rosters WHERE manager_id=? AND player_name=?
              AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
        """, (manager_id, drop_player, gw, gw)).fetchone()
        if not drop_row:
            return False, f"{drop_player} is not on this roster"

    if to_ir and not (drop_row and drop_row['slot_type'] == 'ir'):
        existing_ir = c.execute("""
            SELECT 1 FROM rosters WHERE manager_id=? AND slot_type='ir'
              AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (manager_id, gw, gw)).fetchone()
        if existing_ir:
            return False, "Only one player can be on IR at a time."
    elif not drop_player and not to_ir:
        if count_active_roster_slots(conn, manager_id, gw) >= PLAYER_PICKS_PER_TEAM:
            return False, "Your roster is full — drop a player to submit this claim (or free up a spot by moving someone to IR)."

    return True, None


def submit_or_queue_claim(conn, manager_id, add_player, drop_player, gw, to_ir, season):
    """Land a claim in the currently open waiver window, or -- if none is
    open -- stage it in pending_waiver_claims to be promoted the next time
    one is manually opened (see waiver_open()). We deliberately never
    auto-open a window here: opening one is a single global toggle
    (templates/history.html's Add-vs-Claim button) that would collaterally
    convert every other manager's still-valid instant "Add" for a
    not-yet-played player into a slow "Claim" too. Caller must have already
    run validate_claim_target. Does not commit.
    Returns {"window_open": bool}.
    """
    window = get_open_waiver_window(conn, season)
    if window:
        next_priority = conn.execute("""
            SELECT COALESCE(MAX(priority), 0) + 1 FROM waiver_claims
            WHERE window_id=? AND manager_id=? AND status='pending'
        """, (window['id'], manager_id)).fetchone()[0]
        conn.execute("""
            INSERT INTO waiver_claims (window_id, manager_id, add_player, drop_player, to_ir, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (window['id'], manager_id, add_player, drop_player, 1 if to_ir else 0,
              next_priority, now_eastern_naive().isoformat()))
    else:
        conn.execute("""
            INSERT INTO pending_waiver_claims (season, manager_id, add_player, drop_player, to_ir, gw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (season, manager_id, add_player, drop_player, 1 if to_ir else 0, gw, now_eastern_naive().isoformat()))
    return {"window_open": bool(window)}


def get_waiver_order(conn, season):
    rows = conn.execute("""
        SELECT wo.manager_id, wo.position, m.name, m.team_name
        FROM waiver_order wo JOIN managers m ON m.id = wo.manager_id
        WHERE wo.season=? ORDER BY wo.position
    """, (season,)).fetchall()
    return [dict(r) for r in rows]


def seed_waiver_order_if_needed(conn, season):
    """Initial waiver order = reverse Round 1 draft order, per spec. Seeded
    once, lazily, the first time a window is opened.

    Sourced from draft_picks (the actual, final record of who picked what),
    not draft_order.pick_slot (the pre-draft randomizer slot). Those two
    diverge whenever a Round 1 pick was traded before the draft started —
    pick_slot never moves with a traded pick, only draft_picks.manager_id
    does — so deriving from pick_slot silently produces the wrong order
    whenever a Round 1 trade happened. See scripts/fix_gw1_waiver_order.py
    for a one-off repair of a season already seeded the old, wrong way.
    """
    existing = conn.execute("SELECT 1 FROM waiver_order WHERE season=? LIMIT 1", (season,)).fetchone()
    if existing:
        return
    draft_rows = conn.execute(
        "SELECT manager_id FROM draft_picks WHERE season=? AND round=1 ORDER BY overall_pick DESC", (season,)
    ).fetchall()
    for i, row in enumerate(draft_rows, start=1):
        conn.execute(
            "INSERT INTO waiver_order (season, manager_id, position) VALUES (?, ?, ?)",
            (season, row['manager_id'], i)
        )


@app.route('/api/waiver/open', methods=['POST'])
def waiver_open():
    conn = get_db()
    try:
        season = DRAFT_SEASON
        if get_open_waiver_window(conn, season):
            return jsonify({"error": "A waiver window is already open."}), 409

        seed_waiver_order_if_needed(conn, season)

        next_num = conn.execute(
            "SELECT COALESCE(MAX(window_number), 0) + 1 FROM waiver_windows WHERE season=?", (season,)
        ).fetchone()[0]
        # Purely sequential — each window is "following" the next GW after
        # the last one opened, independent of scraper/scoring timing (no
        # real kickoff-time data exists to derive this automatically).
        gw = conn.execute(
            "SELECT COALESCE(MAX(gw), 0) + 1 FROM waiver_windows WHERE season=?", (season,)
        ).fetchone()[0]

        conn.execute(
            "INSERT INTO waiver_windows (season, window_number, gw, status, opened_at) VALUES (?, ?, ?, 'open', ?)",
            (season, next_num, gw, now_eastern_naive().isoformat())
        )
        window_id = conn.execute(
            "SELECT id FROM waiver_windows WHERE season=? AND window_number=?", (season, next_num)
        ).fetchone()['id']
        log_audit(conn, None, 'waiver', 'open_window', f"Opened waiver window #{next_num} (GW{gw})")

        # Fold in any locked-add pickups that were queued while no window
        # existed (see submit_or_queue_claim) -- re-validate each against
        # current roster state, since it may have drifted since it was
        # queued, rather than trusting the queued row blindly.
        pending_rows = conn.execute(
            "SELECT * FROM pending_waiver_claims WHERE season=? ORDER BY manager_id, id", (season,)
        ).fetchall()
        priority_by_manager = {}
        promoted = 0
        for row in pending_rows:
            ok, err = validate_claim_target(conn, row['manager_id'], row['add_player'], row['drop_player'], gw, bool(row['to_ir']))
            if not ok:
                log_audit(conn, row['manager_id'], 'waiver', 'pending_claim_dropped',
                          f"Could not promote queued claim for {row['add_player']}: {err}")
                continue
            next_priority = priority_by_manager.get(row['manager_id'])
            if next_priority is None:
                next_priority = conn.execute("""
                    SELECT COALESCE(MAX(priority), 0) + 1 FROM waiver_claims
                    WHERE window_id=? AND manager_id=? AND status='pending'
                """, (window_id, row['manager_id'])).fetchone()[0]
            conn.execute("""
                INSERT INTO waiver_claims (window_id, manager_id, add_player, drop_player, to_ir, priority, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (window_id, row['manager_id'], row['add_player'], row['drop_player'],
                  row['to_ir'], next_priority, now_eastern_naive().isoformat()))
            priority_by_manager[row['manager_id']] = next_priority + 1
            promoted += 1
            log_audit(conn, row['manager_id'], 'waiver', 'claim_promoted',
                      f"Queued claim for {row['add_player']} promoted into waiver window #{next_num}",
                      {"add_player": row['add_player'], "drop_player": row['drop_player'], "to_ir": bool(row['to_ir'])})
        conn.execute("DELETE FROM pending_waiver_claims WHERE season=?", (season,))

        conn.commit()
        return jsonify({"status": "ok", "window_number": next_num, "gw": gw, "promoted_pending_claims": promoted})
    except Exception as e:
        conn.rollback()
        print(f"waiver_open error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/claim', methods=['POST'])
def waiver_claim():
    data = request.get_json() or {}
    manager_id  = current_manager_id()
    add_player  = data.get('add_player')
    drop_player = data.get('drop_player') or None
    to_ir       = bool(data.get('to_ir'))
    gw          = data.get('gw')

    if not add_player or not gw:
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()
    # Serialize the priority read + insert below against concurrent claim
    # submissions from the same manager (two tabs, a double-click, etc.) so
    # they can't both read the same MAX(priority) before either commits.
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        season = DRAFT_SEASON
        window = get_open_waiver_window(conn, season)
        if not window:
            conn.execute("ROLLBACK")
            return jsonify({"error": "No waiver window is currently open."}), 409

        ok, err = validate_claim_target(conn, manager_id, add_player, drop_player, gw, to_ir)
        if not ok:
            conn.execute("ROLLBACK")
            status = 404 if 'not on this roster' in err else 409
            return jsonify({"error": err}), status

        next_priority = conn.execute("""
            SELECT COALESCE(MAX(priority), 0) + 1 FROM waiver_claims
            WHERE window_id=? AND manager_id=? AND status='pending'
        """, (window['id'], manager_id)).fetchone()[0]

        conn.execute("""
            INSERT INTO waiver_claims (window_id, manager_id, add_player, drop_player, to_ir, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (window['id'], manager_id, add_player, drop_player, 1 if to_ir else 0, next_priority, now_eastern_naive().isoformat()))
        claim_summary = f"Submitted waiver claim: add {add_player}"
        claim_summary += f", drop {drop_player}" if drop_player else (" — direct to IR" if to_ir else " (no drop — had an open roster spot)")
        log_audit(conn, manager_id, 'waiver', 'claim_submitted', claim_summary,
                  {"add_player": add_player, "drop_player": drop_player, "gw": gw, "to_ir": to_ir})
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"waiver_claim error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/claim/<int:claim_id>/reorder', methods=['POST'])
def waiver_claim_reorder(claim_id):
    data = request.get_json() or {}
    direction = data.get('direction')
    conn = get_db()
    try:
        claim = conn.execute("SELECT * FROM waiver_claims WHERE id=? AND status='pending'", (claim_id,)).fetchone()
        if not claim:
            return jsonify({"error": "Claim not found"}), 404
        if claim['manager_id'] != current_manager_id():
            return jsonify({"error": "Only the manager who submitted this claim can reorder it"}), 403

        if direction == 'up':
            neighbor = conn.execute("""
                SELECT * FROM waiver_claims WHERE window_id=? AND manager_id=? AND status='pending' AND priority < ?
                ORDER BY priority DESC LIMIT 1
            """, (claim['window_id'], claim['manager_id'], claim['priority'])).fetchone()
        elif direction == 'down':
            neighbor = conn.execute("""
                SELECT * FROM waiver_claims WHERE window_id=? AND manager_id=? AND status='pending' AND priority > ?
                ORDER BY priority ASC LIMIT 1
            """, (claim['window_id'], claim['manager_id'], claim['priority'])).fetchone()
        else:
            return jsonify({"error": "Invalid direction"}), 400

        if not neighbor:
            return jsonify({"status": "ok"})

        # Swap through a sentinel that can't collide with the
        # UNIQUE(window_id, manager_id, priority) constraint — going
        # straight to neighbor['priority'] would collide with neighbor's
        # still-current row.
        conn.execute("UPDATE waiver_claims SET priority=-1 WHERE id=?", (claim['id'],))
        conn.execute("UPDATE waiver_claims SET priority=? WHERE id=?", (claim['priority'], neighbor['id']))
        conn.execute("UPDATE waiver_claims SET priority=? WHERE id=?", (neighbor['priority'], claim['id']))
        log_audit(conn, claim['manager_id'], 'waiver', 'reorder_claim',
                  f"Reordered waiver claim for {claim['add_player']} ({direction})")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"waiver_claim_reorder error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/claim/<int:claim_id>', methods=['DELETE'])
def waiver_claim_delete(claim_id):
    conn = get_db()
    try:
        claim = conn.execute("SELECT * FROM waiver_claims WHERE id=? AND status='pending'", (claim_id,)).fetchone()
        if not claim:
            return jsonify({"error": "Claim not found"}), 404
        if claim['manager_id'] != current_manager_id():
            return jsonify({"error": "Only the manager who submitted this claim can cancel it"}), 403
        conn.execute("DELETE FROM waiver_claims WHERE id=?", (claim_id,))
        cancel_summary = f"Cancelled waiver claim: add {claim['add_player']}"
        cancel_summary += f", drop {claim['drop_player']}" if claim['drop_player'] else " (no drop)"
        log_audit(conn, claim['manager_id'], 'waiver', 'cancel_claim', cancel_summary)
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"waiver_claim_delete error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/pending-claim/<int:claim_id>', methods=['DELETE'])
def pending_waiver_claim_delete(claim_id):
    conn = get_db()
    try:
        claim = conn.execute("SELECT * FROM pending_waiver_claims WHERE id=?", (claim_id,)).fetchone()
        if not claim:
            return jsonify({"error": "Claim not found"}), 404
        if claim['manager_id'] != current_manager_id():
            return jsonify({"error": "Only the manager who submitted this claim can cancel it"}), 403
        conn.execute("DELETE FROM pending_waiver_claims WHERE id=?", (claim_id,))
        cancel_summary = f"Cancelled queued waiver claim: add {claim['add_player']}"
        cancel_summary += f", drop {claim['drop_player']}" if claim['drop_player'] else " (no drop)"
        log_audit(conn, claim['manager_id'], 'waiver', 'cancel_pending_claim', cancel_summary)
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"pending_waiver_claim_delete error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/claims/reorder-all', methods=['POST'])
def waiver_claims_reorder_all():
    """Persist a full drag-and-drop reorder: given an ordered list of claim
    ids (all belonging to the same manager/window), reassign priorities
    1..N to match. Two-phase (negative sentinels first) to avoid transient
    collisions with the UNIQUE(window_id, manager_id, priority) constraint."""
    data = request.get_json() or {}
    ordered_ids = data.get('ordered_claim_ids') or []
    if not ordered_ids:
        return jsonify({"error": "Missing ordered_claim_ids"}), 400

    conn = get_db()
    try:
        placeholders = ','.join('?' * len(ordered_ids))
        claims = conn.execute(
            f"SELECT * FROM waiver_claims WHERE id IN ({placeholders}) AND status='pending'",
            ordered_ids
        ).fetchall()
        claim_by_id = {c['id']: c for c in claims}
        if len(claim_by_id) != len(ordered_ids):
            return jsonify({"error": "One or more claims not found or no longer pending"}), 404
        if len({c['manager_id'] for c in claims}) != 1 or len({c['window_id'] for c in claims}) != 1:
            return jsonify({"error": "All claims being reordered must belong to the same manager and window"}), 400
        if claims[0]['manager_id'] != current_manager_id():
            return jsonify({"error": "Only the manager who submitted these claims can reorder them"}), 403

        for i, cid in enumerate(ordered_ids, start=1):
            conn.execute("UPDATE waiver_claims SET priority=? WHERE id=?", (-i, cid))
        for i, cid in enumerate(ordered_ids, start=1):
            conn.execute("UPDATE waiver_claims SET priority=? WHERE id=?", (i, cid))
        log_audit(conn, claims[0]['manager_id'], 'waiver', 'reorder_claims',
                  f"Reordered {len(ordered_ids)} waiver claims")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"waiver_claims_reorder_all error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/waiver/process', methods=['POST'])
def waiver_process():
    """
    Turn-by-turn processing per spec: repeatedly re-scan the waiver order
    from the top for the first manager with an unresolved claim, attempt
    their highest-priority remaining claim, then repeat. Success moves that
    manager to the bottom of the order; failure leaves their position
    unchanged so a follow-up claim (if any) is retried on the next scan.
    """
    data = request.get_json() or {}
    gw = data.get('gw')
    if not gw:
        return jsonify({"error": "Missing gw"}), 400

    conn = get_db()
    try:
        season = DRAFT_SEASON
        window = get_open_waiver_window(conn, season)
        if not window:
            return jsonify({"error": "No waiver window is currently open."}), 409

        order = [r['manager_id'] for r in get_waiver_order(conn, season)]

        pending = {}
        claims = conn.execute("""
            SELECT * FROM waiver_claims WHERE window_id=? AND status='pending' ORDER BY manager_id, priority
        """, (window['id'],)).fetchall()
        for cl in claims:
            pending.setdefault(cl['manager_id'], []).append(dict(cl))

        claimed_this_run = set()
        sequence = 0

        while True:
            next_manager = None
            for m in order:
                if pending.get(m):
                    next_manager = m
                    break
            if next_manager is None:
                break

            claim = pending[next_manager][0]
            sequence += 1

            if claim['add_player'] in claimed_this_run:
                fail_reason = f"{claim['add_player']} was already claimed earlier this window"
                conn.execute(
                    "UPDATE waiver_claims SET status='failed', fail_reason=?, sequence_number=? WHERE id=?",
                    (fail_reason, sequence, claim['id'])
                )
                pending[next_manager].pop(0)
                continue

            ok, info = execute_roster_swap(conn, next_manager, claim['add_player'], claim['drop_player'], gw,
                                            'waiver_claim', to_ir=bool(claim.get('to_ir')))
            if ok:
                conn.execute(
                    "UPDATE waiver_claims SET status='success', sequence_number=? WHERE id=?",
                    (sequence, claim['id'])
                )
                claimed_this_run.add(claim['add_player'])
                pending[next_manager].pop(0)
                order.remove(next_manager)
                order.append(next_manager)
            else:
                conn.execute(
                    "UPDATE waiver_claims SET status='failed', fail_reason=?, sequence_number=? WHERE id=?",
                    (info['error'], sequence, claim['id'])
                )
                pending[next_manager].pop(0)

        conn.execute("DELETE FROM waiver_order WHERE season=?", (season,))
        for i, manager_id in enumerate(order, start=1):
            conn.execute(
                "INSERT INTO waiver_order (season, manager_id, position) VALUES (?, ?, ?)",
                (season, manager_id, i)
            )

        conn.execute(
            "UPDATE waiver_windows SET status='complete', closed_at=? WHERE id=?",
            (now_eastern_naive().isoformat(), window['id'])
        )
        conn.commit()
        return jsonify({"status": "ok", "processed": sequence})
    except Exception as e:
        conn.rollback()
        print(f"waiver_process error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


def get_draft_state(conn):
    row = conn.execute(
        "SELECT * FROM draft_state WHERE season=?", (DRAFT_SEASON,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO draft_state (season, status, spin_count, current_pick_number, rounds, teams_count) "
            "VALUES (?, 'not_started', 0, 0, ?, ?)",
            (DRAFT_SEASON, DRAFT_ROUNDS, DRAFT_TEAMS)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM draft_state WHERE season=?", (DRAFT_SEASON,)
        ).fetchone()
    return row


def get_draft_order_map(conn):
    """pick_slot (1-8) -> manager_id, per the locked randomizer result."""
    rows = conn.execute(
        "SELECT pick_slot, manager_id FROM draft_order WHERE season=? ORDER BY pick_slot",
        (DRAFT_SEASON,)
    ).fetchall()
    return {r['pick_slot']: r['manager_id'] for r in rows}


def pick_to_manager(order_map, overall_pick):
    """Snake order: odd rounds go 1->8, even rounds go 8->1."""
    round_num = (overall_pick - 1) // DRAFT_TEAMS + 1
    pick_in_round = (overall_pick - 1) % DRAFT_TEAMS + 1
    slot = pick_in_round if round_num % 2 == 1 else (DRAFT_TEAMS + 1 - pick_in_round)
    return round_num, pick_in_round, order_map.get(slot)


def resolve_pick_owner(conn, order_map, overall_pick):
    """Same (round_num, pick_in_round, manager_id) shape as pick_to_manager(),
    but honors any accepted pick trade for this pick. No trade -> falls back
    to the plain slot-based owner."""
    round_num, pick_in_round, slot_manager = pick_to_manager(order_map, overall_pick)
    override = conn.execute(
        "SELECT manager_id FROM draft_pick_ownership WHERE season=? AND overall_pick=?",
        (DRAFT_SEASON, overall_pick)
    ).fetchone()
    return round_num, pick_in_round, (override['manager_id'] if override else slot_manager)


def compute_pick_ownership(conn, order_map):
    """overall_pick (1..DRAFT_TOTAL_PICKS) -> {round, pick_in_round, manager_id},
    accounting for accepted trades. Backs both /api/draft/trades and the
    snake matrix's traded-pick annotation."""
    ownership = {}
    for overall_pick in range(1, DRAFT_TOTAL_PICKS + 1):
        round_num, pick_in_round, manager_id = resolve_pick_owner(conn, order_map, overall_pick)
        ownership[overall_pick] = {'round': round_num, 'pick_in_round': pick_in_round, 'manager_id': manager_id}
    return ownership


def auto_assign_slot(conn, manager_id, player_name):
    """
    Best-fit default slot for a freshly drafted player: first open starter
    position they're eligible for, else bench. Manager can rearrange live
    via the side panel afterward (same swap logic as Player Add/Drop).
    """
    eligible = {r[0] for r in conn.execute("""
        SELECT pe.position FROM players p
        JOIN player_eligibility pe ON pe.player_id = p.id
        WHERE p.name = ?
    """, (player_name,)).fetchall()}

    starter_counts = {r['position_slot']: r['cnt'] for r in conn.execute("""
        SELECT position_slot, COUNT(*) as cnt FROM rosters
        WHERE manager_id=? AND slot_type='starter' AND (gw_end IS NULL OR gw_end >= 1)
        GROUP BY position_slot
    """, (manager_id,)).fetchall()}

    for pos in ('FW', 'MID', 'DEF', 'GK'):
        if pos in eligible and starter_counts.get(pos, 0) < POSITION_TARGETS[pos]:
            return 'starter', pos

    return 'bench', (sorted(eligible)[0] if eligible else None)


def compute_full_player_stats(conn):
    """Bulk lookups shared by the Main Draft pool and the Summer/Winter
    Transfer Draft picking tables: 2025-26 season totals, position
    eligibility, full raw-stat sums (one GROUP BY pass), and 2026-27
    projections. Returns (totals_2025, eligibility_by_player, stat_sums, projections)."""
    c = conn.cursor()
    totals_2025 = calc_bulk_season_totals(conn, '2025-26', match_id_filter=(0, SEASON_CUTOFF))

    elig_rows = c.execute("""
        SELECT p.name, pe.position FROM players p
        JOIN player_eligibility pe ON pe.player_id = p.id
    """).fetchall()
    eligibility_by_player = {}
    for r in elig_rows:
        eligibility_by_player.setdefault(r['name'], []).append(r['position'])

    # Season-total raw stats per player for 2025-26, one GROUP BY pass.
    stat_sums = {r['player_name']: dict(r) for r in c.execute("""
        SELECT player_name,
               SUM(goals) AS goals, SUM(assists) AS assists,
               SUM(shots_on_target) AS shots_on_target, SUM(key_passes) AS key_passes,
               SUM(dribbles) AS dribbles, SUM(tackles) AS tackles,
               SUM(interceptions) AS interceptions, SUM(clearances) AS clearances,
               SUM(blocked_shots) AS blocked_shots, SUM(acc_crosses) AS acc_crosses,
               SUM(acc_long_balls) AS acc_long_balls, SUM(saves) AS saves,
               SUM(pk_saves) AS pk_saves, SUM(glc) AS glc, SUM(lmt) AS lmt,
               SUM(elg) AS elg, SUM(own_goals) AS own_goals, SUM(motm) AS motm,
               SUM(yellow_cards) AS yellow_cards, SUM(red_cards) AS red_cards,
               SUM(minutes_played) AS minutes_played
        FROM raw_stats
        WHERE match_id < ?
        GROUP BY player_name
    """, (SEASON_CUTOFF,)).fetchall()}

    projections = {r['player_name']: dict(r) for r in c.execute(
        "SELECT player_name, proj_total, proj_avg FROM player_projections WHERE season=?",
        (DRAFT_SEASON,)
    ).fetchall()}

    return totals_2025, eligibility_by_player, stat_sums, projections


# ── Shortlist ────────────────────────────────────────────────────────────────

@app.route('/api/shortlist/toggle', methods=['POST'])
def toggle_shortlist():
    manager_id = current_manager_id()
    data = request.get_json() or {}
    player_name = data.get('player_name')
    if not player_name:
        return jsonify({"error": "Missing player_name"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM shortlists WHERE manager_id=? AND player_name=?", (manager_id, player_name)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM shortlists WHERE id=?", (existing['id'],))
        toggled_on = False
    else:
        conn.execute(
            "INSERT INTO shortlists (manager_id, player_name, added_at) VALUES (?, ?, ?)",
            (manager_id, player_name, now_eastern_naive().isoformat())
        )
        toggled_on = True

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "on": toggled_on})


@app.route('/draft')
def draft_page():
    conn = get_db()
    c = conn.cursor()
    state = get_draft_state(conn)
    managers = [dict(m) for m in conn.execute(
        "SELECT id, name, team_name, photo_path FROM managers ORDER BY name"
    ).fetchall()]

    order_map = get_draft_order_map(conn)
    managers_by_id = {m['id']: dict(m) for m in managers}
    order = [{'pick_slot': slot, **managers_by_id[order_map[slot]]} for slot in sorted(order_map)] if order_map else []
    manager_names = {m['id']: m['name'] for m in managers}
    manager_teams = {m['id']: m['team_name'] for m in managers}

    browse_players = []
    if state['status'] in ('ready', 'in_progress', 'complete'):
        owner_map = get_owner_map(conn, DRAFT_SEASON, 1)
        totals_2025, eligibility_by_player, stat_sums, projections = compute_full_player_stats(conn)

        rows = c.execute(f"""
            SELECT p.name, p.club FROM players p
            WHERE p.club IS NOT NULL AND p.club != ''
              AND p.club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
              AND p.draftable = 1
            ORDER BY p.name
        """, RELEGATED_CLUBS).fetchall()

        for p in rows:
            owner = owner_map.get(p['name'])
            s25 = totals_2025.get(p['name'], {'total': 0.0, 'avg': 0.0})
            proj = projections.get(p['name'], {})
            browse_players.append({
                'name': p['name'],
                'club': p['club'],
                'eligibility': sorted(eligibility_by_player.get(p['name']) or []),
                'owner_manager_id': owner['manager_id'] if owner else None,
                'owner_name': owner['manager_name'] if owner else None,
                'pts_2025_26': s25['total'],
                'avg_2025_26': s25['avg'],
                'proj_total': proj.get('proj_total'),
                'proj_avg': proj.get('proj_avg'),
                'stats': stat_sums.get(p['name'], {}),
            })

    current_gw = get_current_gw(conn, DRAFT_SEASON)
    all_player_names = [r['name'] for r in conn.execute("SELECT name FROM players ORDER BY name").fetchall()]
    current_pl_clubs = get_current_pl_clubs(conn)
    my_shortlist = get_my_shortlist(conn)
    pick_ownership = compute_pick_ownership(conn, order_map) if order_map else {}
    conn.close()
    return render_template('draft.html',
        state=dict(state),
        managers=managers,
        managers_by_id=managers_by_id,
        order=order,
        browse_players=browse_players,
        season=DRAFT_SEASON,
        total_picks=DRAFT_TOTAL_PICKS,
        position_targets=POSITION_TARGETS,
        scraper_status=read_scraper_status(),
        badges=load_badges(),
        current_gw=current_gw,
        stat_cols=STAT_COLS,
        all_player_names=all_player_names,
        current_pl_clubs=current_pl_clubs,
        my_shortlist=my_shortlist,
        pick_ownership=pick_ownership,
        manager_names=manager_names,
        manager_teams=manager_teams,
        world_cup_emoji_pool=WORLD_CUP_EMOJI_POOL,
    )


@app.route('/api/draft/spin', methods=['POST'])
def draft_spin():
    conn = get_db()
    state = get_draft_state(conn)

    if state['status'] not in ('not_started', 'randomizing'):
        conn.close()
        return jsonify({"error": "Draft order is already locked in."}), 409

    managers = conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()
    shuffled = [dict(m) for m in managers]
    random.shuffle(shuffled)

    spin_count = state['spin_count'] + 1
    locked = spin_count >= 3

    conn.execute(
        "UPDATE draft_state SET status=?, spin_count=? WHERE season=?",
        ('ready' if locked else 'randomizing', spin_count, DRAFT_SEASON)
    )

    if locked:
        conn.execute("DELETE FROM draft_order WHERE season=?", (DRAFT_SEASON,))
        for i, m in enumerate(shuffled, start=1):
            conn.execute(
                "INSERT INTO draft_order (season, pick_slot, manager_id) VALUES (?, ?, ?)",
                (DRAFT_SEASON, i, m['id'])
            )

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "spin_count": spin_count, "locked": locked, "order": shuffled})


@app.route('/api/draft/start', methods=['POST'])
def draft_start():
    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] != 'ready':
        conn.close()
        return jsonify({"error": "Draft order must be locked in before starting."}), 409

    conn.execute(
        "UPDATE draft_state SET status='in_progress', current_pick_number=1 WHERE season=?",
        (DRAFT_SEASON,)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Main Draft pick trades ──────────────────────────────────────────────────
# Trades are only allowed while the order is locked but the draft hasn't
# started (status='ready') — see resolve_pick_owner() for how an accepted
# trade overrides the default slot-based pick owner everywhere else.

@app.route('/api/draft/trades')
def draft_trades_api():
    conn = get_db()
    order_map = get_draft_order_map(conn)
    ownership = compute_pick_ownership(conn, order_map) if order_map else {}

    trades = [dict(t) for t in conn.execute("""
        SELECT t.*, p.name AS proposer_name, tgt.name AS target_name
        FROM draft_pick_trades t
        JOIN managers p ON p.id = t.proposer_manager_id
        JOIN managers tgt ON tgt.id = t.target_manager_id
        WHERE t.season=?
        ORDER BY t.created_at DESC
    """, (DRAFT_SEASON,)).fetchall()]

    items_by_trade = {}
    for it in conn.execute("SELECT * FROM draft_pick_trade_items WHERE trade_id IN (SELECT id FROM draft_pick_trades WHERE season=?)", (DRAFT_SEASON,)).fetchall():
        items_by_trade.setdefault(it['trade_id'], []).append(dict(it))
    for t in trades:
        t['items'] = items_by_trade.get(t['id'], [])

    conn.close()
    return jsonify({"ownership": ownership, "trades": trades})


@app.route('/api/draft/trade/propose', methods=['POST'])
def draft_trade_propose():
    manager_id = current_manager_id()
    data = request.get_json() or {}
    try:
        target_manager_id = int(data.get('target_manager_id'))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing or invalid target_manager_id."}), 400
    give_picks = data.get('give_picks') or []
    receive_picks = data.get('receive_picks') or []

    if target_manager_id == int(manager_id):
        return jsonify({"error": "You can't trade with yourself."}), 400
    if not give_picks and not receive_picks:
        return jsonify({"error": "Select at least one pick to trade."}), 400

    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] != 'ready':
        conn.close()
        return jsonify({"error": "Trades can only be proposed after the draft order is locked and before the draft starts."}), 409

    target = conn.execute("SELECT id, name FROM managers WHERE id=?", (target_manager_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "Unknown manager."}), 400

    order_map = get_draft_order_map(conn)
    items = []
    for overall_pick in give_picks:
        _, _, owner = resolve_pick_owner(conn, order_map, int(overall_pick))
        if owner != int(manager_id):
            conn.close()
            return jsonify({"error": f"You don't currently own pick #{overall_pick}."}), 409
        items.append((int(overall_pick), int(manager_id), target_manager_id))
    for overall_pick in receive_picks:
        _, _, owner = resolve_pick_owner(conn, order_map, int(overall_pick))
        if owner != target_manager_id:
            conn.close()
            return jsonify({"error": f"{target['name']} doesn't currently own pick #{overall_pick}."}), 409
        items.append((int(overall_pick), target_manager_id, int(manager_id)))

    now = now_eastern_naive().isoformat()
    cur = conn.execute(
        "INSERT INTO draft_pick_trades (season, proposer_manager_id, target_manager_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (DRAFT_SEASON, manager_id, target_manager_id, now)
    )
    trade_id = cur.lastrowid
    for overall_pick, from_id, to_id in items:
        conn.execute(
            "INSERT INTO draft_pick_trade_items (trade_id, overall_pick, from_manager_id, to_manager_id) VALUES (?, ?, ?, ?)",
            (trade_id, overall_pick, from_id, to_id)
        )

    log_audit(conn, manager_id, 'draft_trade', 'propose', f"Proposed a pick trade with {target['name']}",
              {"trade_id": trade_id, "give_picks": give_picks, "receive_picks": receive_picks})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "trade_id": trade_id})


def _get_pending_trade(conn, trade_id):
    return conn.execute("SELECT * FROM draft_pick_trades WHERE id=? AND status='pending'", (trade_id,)).fetchone()


@app.route('/api/draft/trade/<int:trade_id>/accept', methods=['POST'])
def draft_trade_accept(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['target_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager this trade was sent to can accept it."}), 403

    state = get_draft_state(conn)
    if state['status'] != 'ready':
        conn.close()
        return jsonify({"error": "The draft has already started — this trade can no longer be applied."}), 409

    order_map = get_draft_order_map(conn)
    items = conn.execute("SELECT * FROM draft_pick_trade_items WHERE trade_id=?", (trade_id,)).fetchall()
    for it in items:
        _, _, owner = resolve_pick_owner(conn, order_map, it['overall_pick'])
        if owner != it['from_manager_id']:
            conn.close()
            return jsonify({"error": f"Pick #{it['overall_pick']} has changed hands since this trade was proposed — ask them to re-propose."}), 409

    for it in items:
        conn.execute(
            "INSERT INTO draft_pick_ownership (season, overall_pick, manager_id) VALUES (?, ?, ?) "
            "ON CONFLICT(season, overall_pick) DO UPDATE SET manager_id=excluded.manager_id",
            (DRAFT_SEASON, it['overall_pick'], it['to_manager_id'])
        )

    conn.execute(
        "UPDATE draft_pick_trades SET status='accepted', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    log_audit(conn, manager_id, 'draft_trade', 'accept', f"Accepted pick trade #{trade_id}", {"trade_id": trade_id})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/draft/trade/<int:trade_id>/decline', methods=['POST'])
def draft_trade_decline(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['target_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager this trade was sent to can decline it."}), 403

    conn.execute(
        "UPDATE draft_pick_trades SET status='declined', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    log_audit(conn, manager_id, 'draft_trade', 'decline', f"Declined pick trade #{trade_id}", {"trade_id": trade_id})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/draft/trade/<int:trade_id>/cancel', methods=['POST'])
def draft_trade_cancel(trade_id):
    manager_id = current_manager_id()
    conn = get_db()
    trade = _get_pending_trade(conn, trade_id)
    if not trade:
        conn.close()
        return jsonify({"error": "Trade not found or already resolved."}), 404
    if trade['proposer_manager_id'] != int(manager_id):
        conn.close()
        return jsonify({"error": "Only the manager who proposed this trade can cancel it."}), 403

    conn.execute(
        "UPDATE draft_pick_trades SET status='cancelled', responded_at=? WHERE id=?",
        (now_eastern_naive().isoformat(), trade_id)
    )
    log_audit(conn, manager_id, 'draft_trade', 'cancel', f"Cancelled pick trade #{trade_id}", {"trade_id": trade_id})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


def get_token_state(conn):
    """
    tokens_by_manager: manager_id -> Summer Transfer token number (1-8,
    assigned in claim order) for managers who have claimed one. Token claims
    are draft_picks rows with slot_type='token'; the number is derived from
    claim order rather than stored, so there's a single source of truth.
    """
    rows = conn.execute("""
        SELECT manager_id FROM draft_picks
        WHERE season=? AND slot_type='token'
        ORDER BY overall_pick
    """, (DRAFT_SEASON,)).fetchall()
    return {r['manager_id']: i + 1 for i, r in enumerate(rows)}


def get_player_pick_counts(conn):
    """manager_id -> number of *player* picks made (tokens excluded)."""
    rows = conn.execute("""
        SELECT manager_id, COUNT(*) AS cnt FROM draft_picks
        WHERE season=? AND slot_type != 'token'
        GROUP BY manager_id
    """, (DRAFT_SEASON,)).fetchall()
    return {r['manager_id']: r['cnt'] for r in rows}


@app.route('/api/draft/state')
def draft_state_api():
    conn = get_db()
    state = get_draft_state(conn)
    order_map = get_draft_order_map(conn)

    picks = conn.execute("""
        SELECT dp.*, m.name AS manager_name, m.team_name AS manager_team
        FROM draft_picks dp
        JOIN managers m ON m.id = dp.manager_id
        WHERE dp.season=?
        ORDER BY dp.overall_pick
    """, (DRAFT_SEASON,)).fetchall()

    tokens_by_manager = get_token_state(conn)
    player_pick_counts = get_player_pick_counts(conn)

    on_the_clock = None
    if state['status'] == 'in_progress' and order_map:
        round_num, pick_in_round, manager_id = resolve_pick_owner(conn, order_map, state['current_pick_number'])
        must_claim = (
            player_pick_counts.get(manager_id, 0) >= PLAYER_PICKS_PER_TEAM
            and manager_id not in tokens_by_manager
        )
        on_the_clock = {
            'round': round_num,
            'pick_in_round': pick_in_round,
            'manager_id': manager_id,
            'must_claim_token': must_claim,
        }

    club_by_player = {r['name']: r['club'] for r in conn.execute("SELECT name, club FROM players").fetchall()}
    elig_by_player = {}
    for r in conn.execute("""
        SELECT p.name, pe.position FROM players p
        JOIN player_eligibility pe ON pe.player_id = p.id
    """).fetchall():
        elig_by_player.setdefault(r['name'], []).append(r['position'])

    rosters_by_manager = {}
    roster_rows = conn.execute("""
        SELECT manager_id, player_name, slot_type, position_slot
        FROM rosters
        WHERE gw_end IS NULL OR gw_end >= 1
        ORDER BY slot_type, position_slot
    """).fetchall()
    for r in roster_rows:
        row = dict(r)
        row['club'] = club_by_player.get(r['player_name'])
        row['eligibility'] = sorted(elig_by_player.get(r['player_name']) or [])
        rosters_by_manager.setdefault(r['manager_id'], []).append(row)

    conn.close()
    return jsonify({
        "status": state['status'],
        "spin_count": state['spin_count'],
        "current_pick_number": state['current_pick_number'],
        "total_picks": DRAFT_TOTAL_PICKS,
        "on_the_clock": on_the_clock,
        "picks": [dict(p) for p in picks],
        "rosters_by_manager": rosters_by_manager,
        "tokens_by_manager": tokens_by_manager,
        "player_pick_counts": player_pick_counts,
    })


@app.route('/api/draft/pick', methods=['POST'])
def draft_pick():
    data = request.get_json() or {}
    manager_id = current_manager_id()
    player_name = data.get('player_name')

    conn = get_db()
    state = get_draft_state(conn)

    if state['status'] != 'in_progress':
        conn.close()
        return jsonify({"error": "Draft is not in progress."}), 409

    order_map = get_draft_order_map(conn)
    overall_pick = state['current_pick_number']
    round_num, pick_in_round, expected_manager_id = resolve_pick_owner(conn, order_map, overall_pick)

    if int(manager_id) != expected_manager_id:
        conn.close()
        return jsonify({"error": "It's not your turn to pick."}), 409

    already = conn.execute(
        "SELECT 1 FROM draft_picks WHERE season=? AND player_name=?",
        (DRAFT_SEASON, player_name)
    ).fetchone()
    if already:
        conn.close()
        return jsonify({"error": f"{player_name} has already been drafted."}), 409

    # A manager drafts at most 15 players; the 16th turn is the Summer
    # Transfer token. Once they hit 15 players without a token, their
    # remaining turn can only be a token claim.
    player_counts = get_player_pick_counts(conn)
    tokens = get_token_state(conn)
    if player_counts.get(int(manager_id), 0) >= PLAYER_PICKS_PER_TEAM:
        conn.close()
        if int(manager_id) not in tokens:
            return jsonify({"error": "You already have 15 players — you must claim your Summer Transfer token with this pick."}), 409
        return jsonify({"error": "Your roster is full (15 players + token)."}), 409

    slot_type, position_slot = auto_assign_slot(conn, manager_id, player_name)

    conn.execute("""
        INSERT INTO draft_picks (season, overall_pick, round, pick_in_round, manager_id,
                                  player_name, slot_type, position_slot, picked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (DRAFT_SEASON, overall_pick, round_num, pick_in_round, manager_id,
          player_name, slot_type, position_slot, now_eastern_naive().isoformat()))

    conn.execute("""
        INSERT INTO rosters (manager_id, player_name, slot_type, position_slot, gw_start, gw_end)
        VALUES (?, ?, ?, ?, 1, NULL)
    """, (manager_id, player_name, slot_type, position_slot))

    next_pick = overall_pick + 1
    new_status = 'complete' if next_pick > DRAFT_TOTAL_PICKS else 'in_progress'
    conn.execute(
        "UPDATE draft_state SET current_pick_number=?, status=? WHERE season=?",
        (next_pick, new_status, DRAFT_SEASON)
    )

    log_audit(conn, manager_id, 'draft', 'pick', f"Drafted {player_name} (pick #{overall_pick})",
              {"player": player_name, "overall_pick": overall_pick})
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "slot_type": slot_type, "position_slot": position_slot})


@app.route('/api/draft/claim_token', methods=['POST'])
def draft_claim_token():
    """
    Spend the current turn on a Summer Transfer token instead of a player.
    Token numbers 1-8 are auto-assigned in claim order (first claimer gets
    #1). Every manager must claim exactly one across their 16 turns.
    """
    manager_id = current_manager_id()

    conn = get_db()
    state = get_draft_state(conn)

    if state['status'] != 'in_progress':
        conn.close()
        return jsonify({"error": "Draft is not in progress."}), 409

    order_map = get_draft_order_map(conn)
    overall_pick = state['current_pick_number']
    round_num, pick_in_round, expected_manager_id = resolve_pick_owner(conn, order_map, overall_pick)

    if int(manager_id) != expected_manager_id:
        conn.close()
        return jsonify({"error": "It's not your turn to pick."}), 409

    tokens = get_token_state(conn)
    if int(manager_id) in tokens:
        conn.close()
        return jsonify({"error": "You already claimed your Summer Transfer token."}), 409

    token_number = len(tokens) + 1
    conn.execute("""
        INSERT INTO draft_picks (season, overall_pick, round, pick_in_round, manager_id,
                                  player_name, slot_type, position_slot, picked_at)
        VALUES (?, ?, ?, ?, ?, ?, 'token', NULL, ?)
    """, (DRAFT_SEASON, overall_pick, round_num, pick_in_round, manager_id,
          f"⚡ Summer Transfer #{token_number}", now_eastern_naive().isoformat()))

    next_pick = overall_pick + 1
    new_status = 'complete' if next_pick > DRAFT_TOTAL_PICKS else 'in_progress'
    conn.execute(
        "UPDATE draft_state SET current_pick_number=?, status=? WHERE season=?",
        (next_pick, new_status, DRAFT_SEASON)
    )

    log_audit(conn, manager_id, 'draft', 'claim_token', f"Claimed Summer Transfer token #{token_number}")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "token_number": token_number})


@app.route('/api/draft/roster/slot', methods=['POST'])
def draft_roster_slot():
    """Rearrange a drafted player's slot live during/after the draft (mirrors /api/roster/update)."""
    data = request.get_json() or {}
    manager_id = data.get('manager_id')
    player_name = data.get('player_name')
    new_slot_type = data.get('slot_type')
    new_position_slot = data.get('position_slot')

    if not all([manager_id, player_name, new_slot_type, new_position_slot]):
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] == 'locked':
        conn.close()
        return jsonify({"error": "Draft is locked."}), 409

    row = conn.execute("""
        SELECT id, slot_type, position_slot FROM rosters
        WHERE manager_id=? AND player_name=? AND (gw_end IS NULL OR gw_end >= 1)
    """, (manager_id, player_name)).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Player not found on this roster"}), 404

    conn.execute(
        "UPDATE rosters SET slot_type=?, position_slot=? WHERE id=?",
        (new_slot_type, new_position_slot, row['id'])
    )
    log_audit(conn, manager_id, 'draft', 'lineup_change',
              f"Moved {player_name}: {row['slot_type']} ({row['position_slot']}) → {new_slot_type} ({new_position_slot})")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/draft/lock', methods=['POST'])
def draft_lock():
    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] != 'complete':
        conn.close()
        return jsonify({"error": "Only a completed draft can be locked."}), 409
    conn.execute("UPDATE draft_state SET status='locked' WHERE season=?", (DRAFT_SEASON,))
    log_audit(conn, None, 'draft', 'lock', "Locked the Main Draft")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/draft/unlock', methods=['POST'])
def draft_unlock():
    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] != 'locked':
        conn.close()
        return jsonify({"error": "Draft is not locked."}), 409
    conn.execute("UPDATE draft_state SET status='complete' WHERE season=?", (DRAFT_SEASON,))
    log_audit(conn, None, 'draft', 'unlock', "Unlocked the Main Draft")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/api/draft/reset', methods=['POST'])
def draft_reset():
    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] == 'locked':
        conn.close()
        return jsonify({"error": "Draft is locked — unlock it first if you really want to reset."}), 409
    conn.execute("DELETE FROM draft_state WHERE season=?", (DRAFT_SEASON,))
    conn.execute("DELETE FROM draft_order WHERE season=?", (DRAFT_SEASON,))
    conn.execute("DELETE FROM draft_picks WHERE season=?", (DRAFT_SEASON,))
    conn.execute("DELETE FROM draft_pick_ownership WHERE season=?", (DRAFT_SEASON,))
    conn.execute("DELETE FROM draft_pick_trade_items WHERE trade_id IN (SELECT id FROM draft_pick_trades WHERE season=?)", (DRAFT_SEASON,))
    conn.execute("DELETE FROM draft_pick_trades WHERE season=?", (DRAFT_SEASON,))
    conn.execute("DELETE FROM rosters")
    log_audit(conn, None, 'draft', 'reset', "Reset the Main Draft (wiped all rosters)")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/draft/recap')
def draft_recap():
    conn = get_db()
    state = get_draft_state(conn)

    picks = conn.execute("""
        SELECT dp.*, m.name AS manager_name, m.team_name AS manager_team, m.photo_path
        FROM draft_picks dp
        JOIN managers m ON m.id = dp.manager_id
        WHERE dp.season=?
        ORDER BY dp.overall_pick
    """, (DRAFT_SEASON,)).fetchall()

    picks_by_manager = {}
    for p in picks:
        picks_by_manager.setdefault(p['manager_id'], []).append(dict(p))

    managers = conn.execute(
        "SELECT id, name, team_name, photo_path FROM managers ORDER BY name"
    ).fetchall()

    pick_comments_rows = conn.execute(
        "SELECT * FROM draft_comments WHERE season=? AND target_type='pick' ORDER BY created_at",
        (DRAFT_SEASON,)
    ).fetchall()
    pick_comments = {}
    for r in pick_comments_rows:
        pick_comments.setdefault(r['target_id'], []).append(dict(r))

    team_comments_rows = conn.execute(
        "SELECT * FROM draft_comments WHERE season=? AND target_type='team' ORDER BY created_at",
        (DRAFT_SEASON,)
    ).fetchall()
    team_comments = {}
    for r in team_comments_rows:
        team_comments.setdefault(r['target_id'], []).append(dict(r))

    conn.close()
    return render_template('draft_recap.html',
        state=dict(state),
        managers=managers,
        picks_by_manager=picks_by_manager,
        pick_comments=pick_comments,
        team_comments=team_comments,
        season=DRAFT_SEASON,
        scraper_status=read_scraper_status(),
    )


@app.route('/api/draft/comment', methods=['POST'])
def draft_comment():
    data = request.get_json() or {}
    target_type = data.get('target_type')
    target_id = data.get('target_id')
    comment = (data.get('comment') or '').strip()

    if target_type not in ('pick', 'team') or not target_id or not comment:
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()
    author = conn.execute("SELECT name FROM managers WHERE id=?", (current_manager_id(),)).fetchone()
    author_name = author['name'] if author else 'Unknown'
    conn.execute("""
        INSERT INTO draft_comments (season, target_type, target_id, author_name, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (DRAFT_SEASON, target_type, target_id, author_name, comment, now_eastern_naive().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Transfer Draft (Summer/Winter) ────────────────────────────────────────────

TRANSFER_TYPES = ('summer', 'winter')


def get_transfer_draft(conn, season, draft_type):
    # INSERT OR IGNORE sidesteps the race where two concurrent requests both
    # see "no row yet" and both try to INSERT — the second would otherwise
    # hit the UNIQUE(season, draft_type) constraint.
    conn.execute(
        "INSERT OR IGNORE INTO transfer_drafts (season, draft_type, status, round, current_pick_number) VALUES (?,?,'not_started',1,0)",
        (season, draft_type)
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM transfer_drafts WHERE season=? AND draft_type=?", (season, draft_type)
    ).fetchone()


def get_transfer_draft_order(conn, transfer_draft_id, round_num):
    rows = conn.execute("""
        SELECT tdo.position, tdo.manager_id, m.name, m.team_name
        FROM transfer_draft_order tdo JOIN managers m ON m.id = tdo.manager_id
        WHERE tdo.transfer_draft_id=? AND tdo.round=?
        ORDER BY tdo.position
    """, (transfer_draft_id, round_num)).fetchall()
    return [dict(r) for r in rows]


def get_transfer_pool(conn, season, draft_type):
    rows = conn.execute("""
        SELECT tp.player_name AS name, p.club, p.position, tp.previous_club
        FROM transfer_pool tp JOIN players p ON p.name = tp.player_name
        WHERE tp.season=? AND tp.draft_type=?
        ORDER BY tp.player_name
    """, (season, draft_type)).fetchall()
    return [dict(r) for r in rows]


def transfer_current_pool_query(conn, draft_type, round_num):
    """Round 1 offers only the commissioner-curated transfer_pool; round 2
    opens up to any currently-unrostered player league-wide (including
    anyone dropped earlier in round 1 of this same draft). Both rounds carry
    the same full stat columns as the Main Draft pool table."""
    season = DRAFT_SEASON
    owner_map = get_owner_map(conn, season, get_current_gw(conn, season))
    totals_2025, eligibility_by_player, stat_sums, projections = compute_full_player_stats(conn)

    if round_num == 1:
        rows = conn.execute("""
            SELECT tp.player_name AS name, p.club, p.position, tp.previous_club
            FROM transfer_pool tp JOIN players p ON p.name = tp.player_name
            WHERE tp.season=? AND tp.draft_type=?
            ORDER BY tp.player_name
        """, (season, draft_type)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT p.name, p.club, p.position, NULL AS previous_club FROM players p
            WHERE p.club IS NOT NULL AND p.club != ''
              AND p.club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
              AND p.draftable = 1
            ORDER BY p.name
        """, RELEGATED_CLUBS).fetchall()

    out = []
    for r in rows:
        name = r['name']
        if name in owner_map:
            continue
        s25 = totals_2025.get(name, {'total': 0.0, 'avg': 0.0})
        proj = projections.get(name, {})
        out.append({
            'name': name,
            'club': r['club'],
            'previous_club': r['previous_club'],
            'position': r['position'],
            'eligibility': sorted(eligibility_by_player.get(name) or ([r['position']] if r['position'] else [])),
            'pts_2025_26': s25['total'],
            'avg_2025_26': s25['avg'],
            'proj_total': proj.get('proj_total'),
            'proj_avg': proj.get('proj_avg'),
            'stats': stat_sums.get(name, {}),
        })
    return out


@app.route('/api/transfer/<draft_type>/pool/add', methods=['POST'])
def transfer_pool_add(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    data = request.get_json() or {}
    player_name = (data.get('player_name') or '').strip()
    new_club = (data.get('new_club') or '').strip()
    added_by = data.get('added_by') or ''
    if not player_name:
        return jsonify({"error": "Missing player_name"}), 400

    conn = get_db()
    try:
        player = conn.execute("SELECT club FROM players WHERE name=?", (player_name,)).fetchone()
        if not player:
            return jsonify({"error": f"{player_name} isn't in the players database yet — use \"add new player\" instead."}), 404
        draft = get_transfer_draft(conn, DRAFT_SEASON, draft_type)
        if draft['status'] != 'not_started':
            return jsonify({"error": "Pool can only be edited before the draft starts."}), 409

        # If a corrected club is supplied and differs from what we have on
        # file, update it globally (this player really did transfer — every
        # other page should reflect their new club too) and remember the old
        # club so the pool can show "was {previous_club}". Reject anything
        # that isn't one of the real, current clubs — this field previously
        # accepted any free text, which is exactly how a typo ("Manchester
        # Cit") ended up corrupting a real player's club league-wide.
        previous_club = None
        if new_club and new_club != player['club']:
            current_clubs = get_current_pl_clubs(conn)
            if new_club not in current_clubs:
                return jsonify({"error": f"\"{new_club}\" isn't a current Premier League club. Pick one from the list."}), 400
            previous_club = player['club']
            conn.execute("UPDATE players SET club=? WHERE name=?", (new_club, player_name))

        conn.execute("""
            INSERT INTO transfer_pool (season, draft_type, player_name, previous_club, added_by, added_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(season, draft_type, player_name) DO UPDATE SET
                previous_club=COALESCE(excluded.previous_club, transfer_pool.previous_club)
        """, (DRAFT_SEASON, draft_type, player_name, previous_club, added_by, now_eastern_naive().isoformat()))
        log_audit(conn, None, 'transfer_draft', 'pool_add',
                  f"Added {player_name} to the {draft_type} transfer pool (by {added_by or 'unknown'})")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_pool_add error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/transfer/<draft_type>/pool/add-new', methods=['POST'])
def transfer_pool_add_new(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    data = request.get_json() or {}
    player_name = (data.get('player_name') or '').strip()
    club = (data.get('club') or '').strip()
    position = (data.get('position') or '').strip().upper()
    eligibility = data.get('eligibility') or []
    added_by = data.get('added_by') or ''

    if not player_name or not club or not position:
        return jsonify({"error": "Missing player_name, club, or position"}), 400
    if position not in ('GK', 'DEF', 'MID', 'FW'):
        return jsonify({"error": f"\"{position}\" isn't a valid position (GK/DEF/MID/FW)."}), 400

    conn = get_db()
    try:
        draft = get_transfer_draft(conn, DRAFT_SEASON, draft_type)
        if draft['status'] != 'not_started':
            return jsonify({"error": "Pool can only be edited before the draft starts."}), 409

        current_clubs = get_current_pl_clubs(conn)
        if club not in current_clubs:
            return jsonify({"error": f"\"{club}\" isn't a current Premier League club. Pick one from the list."}), 400

        existing = conn.execute("SELECT id FROM players WHERE name=?", (player_name,)).fetchone()
        if existing:
            player_id = existing[0]
            conn.execute("UPDATE players SET club=?, position=? WHERE id=?", (club, position, player_id))
        else:
            conn.execute("INSERT INTO players (name, club, position) VALUES (?,?,?)", (player_name, club, position))
            player_id = conn.execute("SELECT id FROM players WHERE name=?", (player_name,)).fetchone()[0]

        # The primary position must always be one of the eligible positions
        # — guaranteed here regardless of what the client sent.
        elig_positions = set(eligibility) | {position} if eligibility else {position}
        conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (player_id,))
        for pos in elig_positions:
            conn.execute(
                "INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                (player_id, pos, 'manual')
            )

        conn.execute("""
            INSERT OR IGNORE INTO transfer_pool (season, draft_type, player_name, added_by, added_at)
            VALUES (?,?,?,?,?)
        """, (DRAFT_SEASON, draft_type, player_name, added_by, now_eastern_naive().isoformat()))
        log_audit(conn, None, 'transfer_draft', 'pool_add_new',
                  f"Created new player {player_name} ({club}, {position}) and added to the {draft_type} transfer pool (by {added_by or 'unknown'})")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_pool_add_new error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/transfer/<draft_type>/pool/<player_name>', methods=['DELETE'])
def transfer_pool_remove(draft_type, player_name):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    conn = get_db()
    try:
        draft = get_transfer_draft(conn, DRAFT_SEASON, draft_type)
        if draft['status'] != 'not_started':
            return jsonify({"error": "Pool can only be edited before the draft starts."}), 409
        conn.execute(
            "DELETE FROM transfer_pool WHERE season=? AND draft_type=? AND player_name=?",
            (DRAFT_SEASON, draft_type, player_name)
        )
        log_audit(conn, None, 'transfer_draft', 'pool_remove',
                  f"Removed {player_name} from the {draft_type} transfer pool")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_pool_remove error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/transfer/<draft_type>/start', methods=['POST'])
def transfer_draft_start(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400

    conn = get_db()
    try:
        season = DRAFT_SEASON
        draft = get_transfer_draft(conn, season, draft_type)
        if draft['status'] != 'not_started':
            return jsonify({"error": "This draft has already started."}), 409

        pool_count = conn.execute(
            "SELECT COUNT(*) FROM transfer_pool WHERE season=? AND draft_type=?", (season, draft_type)
        ).fetchone()[0]
        if pool_count == 0:
            return jsonify({"error": "Add at least one player to the pool before starting."}), 409

        if draft_type == 'summer':
            round1 = [r[0] for r in conn.execute("""
                SELECT manager_id FROM draft_picks
                WHERE season=? AND slot_type='token' ORDER BY overall_pick ASC
            """, (season,)).fetchall()]
            if len(round1) < DRAFT_TEAMS:
                return jsonify({"error": "Not every manager has claimed their Summer Transfer token yet."}), 409
        else:
            round1 = [r[0] for r in conn.execute("""
                SELECT m.id
                FROM managers m
                LEFT JOIN (
                    SELECT r.* FROM results r JOIN gameweeks g ON g.id = r.gw_id WHERE g.season=?
                ) res ON res.manager_id = m.id
                LEFT JOIN results opp ON opp.matchup_id = res.matchup_id AND opp.manager_id != m.id
                GROUP BY m.id
                ORDER BY COALESCE(SUM(res.win),0) ASC, COALESCE(SUM(res.fantasy_score),0.0) ASC
            """, (season,)).fetchall()]

        round2 = [r['manager_id'] for r in get_waiver_order(conn, season)]
        if len(round2) < DRAFT_TEAMS:
            return jsonify({"error": "Waiver order isn't set up yet — open at least one waiver window first."}), 409

        conn.execute("DELETE FROM transfer_draft_order WHERE transfer_draft_id=?", (draft['id'],))
        for i, manager_id in enumerate(round1, start=1):
            conn.execute(
                "INSERT INTO transfer_draft_order (transfer_draft_id, round, position, manager_id) VALUES (?,1,?,?)",
                (draft['id'], i, manager_id)
            )
        for i, manager_id in enumerate(round2, start=1):
            conn.execute(
                "INSERT INTO transfer_draft_order (transfer_draft_id, round, position, manager_id) VALUES (?,2,?,?)",
                (draft['id'], i, manager_id)
            )

        conn.execute(
            "UPDATE transfer_drafts SET status='in_progress', round=1, current_pick_number=1, started_at=? WHERE id=?",
            (now_eastern_naive().isoformat(), draft['id'])
        )
        log_audit(conn, None, 'transfer_draft', 'start', f"Started the {draft_type} transfer draft")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_draft_start error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


def transfer_draft_advance(conn, draft):
    """Move current_pick_number forward; roll from round 1 into round 2;
    mark complete after round 2's last turn."""
    next_pick = draft['current_pick_number'] + 1
    if draft['round'] == 1 and next_pick > DRAFT_TEAMS:
        new_round, new_pick, new_status = 2, 1, 'in_progress'
    elif draft['round'] == 2 and next_pick > DRAFT_TEAMS:
        new_round, new_pick, new_status = 2, draft['current_pick_number'], 'complete'
    else:
        new_round, new_pick, new_status = draft['round'], next_pick, 'in_progress'

    completed_at = now_eastern_naive().isoformat() if new_status == 'complete' else None
    conn.execute(
        "UPDATE transfer_drafts SET round=?, current_pick_number=?, status=?, completed_at=COALESCE(?, completed_at) WHERE id=?",
        (new_round, new_pick, new_status, completed_at, draft['id'])
    )


@app.route('/api/transfer/<draft_type>/pick', methods=['POST'])
def transfer_draft_pick(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    data = request.get_json() or {}
    manager_id = current_manager_id()
    player_name = data.get('player_name')
    dropped_player = data.get('dropped_player')
    gw = data.get('gw')

    if not all([player_name, dropped_player, gw]):
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()
    try:
        season = DRAFT_SEASON
        draft = get_transfer_draft(conn, season, draft_type)
        if draft['status'] != 'in_progress':
            return jsonify({"error": "This draft is not in progress."}), 409

        order = get_transfer_draft_order(conn, draft['id'], draft['round'])
        expected = next((o for o in order if o['position'] == draft['current_pick_number']), None)
        if not expected or int(manager_id) != expected['manager_id']:
            return jsonify({"error": "It's not your turn to pick."}), 409

        pool = transfer_current_pool_query(conn, draft_type, draft['round'])
        if player_name not in {p['name'] for p in pool}:
            return jsonify({"error": f"{player_name} is not available to pick this round."}), 409

        ok, info = execute_roster_swap(conn, manager_id, player_name, dropped_player, gw, f'{draft_type}_transfer_draft')
        if not ok:
            return jsonify(info), 409

        overall_pick = (draft['round'] - 1) * DRAFT_TEAMS + draft['current_pick_number']
        conn.execute("""
            INSERT INTO transfer_draft_picks (transfer_draft_id, round, overall_pick, manager_id, player_name, dropped_player, is_pass, picked_at)
            VALUES (?,?,?,?,?,?,0,?)
        """, (draft['id'], draft['round'], overall_pick, manager_id, player_name, dropped_player, now_eastern_naive().isoformat()))

        if draft['round'] == 2:
            live_order = [r['manager_id'] for r in get_waiver_order(conn, season)]
            if int(manager_id) in live_order:
                live_order.remove(int(manager_id))
                live_order.append(int(manager_id))
                conn.execute("DELETE FROM waiver_order WHERE season=?", (season,))
                for i, mid in enumerate(live_order, start=1):
                    conn.execute(
                        "INSERT INTO waiver_order (season, manager_id, position) VALUES (?,?,?)",
                        (season, mid, i)
                    )

        transfer_draft_advance(conn, draft)
        log_audit(conn, manager_id, 'transfer_draft', 'pick',
                  f"{draft_type.capitalize()} transfer draft: picked {player_name}, dropped {dropped_player} (round {draft['round']})")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_draft_pick error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/transfer/<draft_type>/pass', methods=['POST'])
def transfer_draft_pass(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    manager_id = current_manager_id()

    conn = get_db()
    try:
        season = DRAFT_SEASON
        draft = get_transfer_draft(conn, season, draft_type)
        if draft['status'] != 'in_progress':
            return jsonify({"error": "This draft is not in progress."}), 409

        order = get_transfer_draft_order(conn, draft['id'], draft['round'])
        expected = next((o for o in order if o['position'] == draft['current_pick_number']), None)
        if not expected or int(manager_id) != expected['manager_id']:
            return jsonify({"error": "It's not your turn to pick."}), 409

        overall_pick = (draft['round'] - 1) * DRAFT_TEAMS + draft['current_pick_number']
        conn.execute("""
            INSERT INTO transfer_draft_picks (transfer_draft_id, round, overall_pick, manager_id, player_name, dropped_player, is_pass, picked_at)
            VALUES (?,?,?,?,NULL,NULL,1,?)
        """, (draft['id'], draft['round'], overall_pick, manager_id, now_eastern_naive().isoformat()))

        transfer_draft_advance(conn, draft)
        log_audit(conn, manager_id, 'transfer_draft', 'pass',
                  f"{draft_type.capitalize()} transfer draft: passed (round {draft['round']})")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        print(f"transfer_draft_pass error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/transfer/<draft_type>/state')
def transfer_draft_state(draft_type):
    if draft_type not in TRANSFER_TYPES:
        return jsonify({"error": "Invalid draft type"}), 400
    conn = get_db()
    season = DRAFT_SEASON
    draft = get_transfer_draft(conn, season, draft_type)
    pool = get_transfer_pool(conn, season, draft_type)
    order1 = get_transfer_draft_order(conn, draft['id'], 1)
    order2 = get_transfer_draft_order(conn, draft['id'], 2)
    current_pool = transfer_current_pool_query(conn, draft_type, draft['round']) if draft['status'] == 'in_progress' else []
    picks = [dict(p) for p in conn.execute("""
        SELECT tdp.*, m.name AS manager_name, m.team_name
        FROM transfer_draft_picks tdp JOIN managers m ON m.id = tdp.manager_id
        WHERE tdp.transfer_draft_id=?
        ORDER BY tdp.overall_pick
    """, (draft['id'],)).fetchall()]
    conn.close()
    return jsonify({
        "status": draft['status'],
        "round": draft['round'],
        "current_pick_number": draft['current_pick_number'],
        "pool": pool,
        "order_round1": order1,
        "order_round2": order2,
        "current_pool": current_pool,
        "picks": picks,
    })


# ── Audit History ────────────────────────────────────────────────────────────

AUDIT_PAGE_SIZE = 50


def _fetch_audit_entries(conn, manager_id=None, date_from=None, date_to=None,
                          before_created_at=None, before_id=None, limit=AUDIT_PAGE_SIZE):
    where = []
    params = []
    if manager_id:
        where.append("manager_id=?")
        params.append(manager_id)
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("created_at <= ?")
        params.append(date_to)
    if before_created_at is not None and before_id is not None:
        where.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend([before_created_at, before_created_at, before_id])

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"""
        SELECT * FROM audit_log {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


@app.route('/playoffs')
def playoffs_page():
    return render_template('playoffs.html', scraper_status=read_scraper_status())


@app.route('/audit-history')
def audit_history():
    conn = get_db()
    entries = _fetch_audit_entries(conn)
    managers = [dict(r) for r in conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()]
    conn.close()
    has_more = len(entries) == AUDIT_PAGE_SIZE
    return render_template('audit_history.html', entries=entries, managers=managers,
                            has_more=has_more, scraper_status=read_scraper_status())


@app.route('/audit-history/feed')
def audit_history_feed():
    conn = get_db()
    entries = _fetch_audit_entries(
        conn,
        manager_id=request.args.get('manager_id', type=int),
        date_from=request.args.get('date_from') or None,
        date_to=request.args.get('date_to') or None,
        before_created_at=request.args.get('before_created_at'),
        before_id=request.args.get('before_id', type=int),
    )
    conn.close()
    has_more = len(entries) == AUDIT_PAGE_SIZE
    return jsonify({"entries": entries, "has_more": has_more})


# ── Scraper API ───────────────────────────────────────────────────────────────

SCRAPER_STALE_MINUTES = 45  # a "running" status older than this has no real process behind it


def scrape_watchdog(proc, killed_event):
    """Force-kills a scrape's whole process group if it's still alive after
    SCRAPER_STALE_MINUTES. A single match is already bounded (scrape_gw.py's
    own 120s-per-match timeout, itself process-group-enforced — see
    run_match_scrape() in scrape_gw.py), so a run still going this long past
    a normal ~20min gameweek scrape means something is genuinely stuck
    rather than just slow, and left running it's exactly what starved the
    container of memory and took the site down. SIGTERM (not SIGKILL) gives
    the tree a chance to exit cleanly first; run_scraper_background reports
    it as a watchdog kill via `killed_event` rather than a real cancel."""
    time.sleep(SCRAPER_STALE_MINUTES * 60)
    if proc.poll() is not None:
        return  # already finished on its own
    killed_event.set()
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass


def start_scrape(gw, trigger='manual', match_ids=None):
    """Shared by the manual 'Press the Button' route and the auto-scrape
    poller — launches the subprocess, writes the running status (with its
    pid, atomically — see run_scraper_background's docstring for why),
    then hands the already-running proc off to a background thread to
    stream and report on. Caller is responsible for checking the scraper
    isn't already running.

    match_ids, if given, scrapes only those fixtures instead of the whole
    gw — lets someone re-run just the match(es) they care about instead of
    waiting on every fixture in the gameweek."""
    cmd = [sys.executable, 'scrape_gw.py', '--gw', str(gw), '--season', '2026-27']
    if match_ids:
        cmd += ['--match_ids', ','.join(str(m) for m in match_ids)]
    proc = subprocess.Popen(
        cmd,
        cwd=SCRIPTS_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # Its own process group (not just its own process) so cancel can
        # kill the whole tree in one shot — scrape_gw.py's Playwright
        # browser is a child process that a plain kill of just the Python
        # pid would leave orphaned and still running.
        preexec_fn=os.setsid,
    )
    started_at = now_eastern_naive().strftime('%b %d, %Y at %-I:%M %p')
    write_scraper_status({
        "status":       "running",
        "started_at":   started_at,
        "started_at_iso": now_eastern_naive().isoformat(),
        "completed_at": None,
        "gw":           gw,
        "pid":          proc.pid,
    })
    watchdog_killed = threading.Event()
    t = threading.Thread(target=run_scraper_background, args=(proc, gw, started_at, trigger, watchdog_killed))
    t.daemon = True
    t.start()
    w = threading.Thread(target=scrape_watchdog, args=(proc, watchdog_killed))
    w.daemon = True
    w.start()
    return started_at


@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    status = read_scraper_status()
    if status.get('status') == 'running':
        started_iso = status.get('started_at_iso')
        is_stale = False
        if started_iso:
            try:
                age = now_eastern_naive() - datetime.fromisoformat(started_iso)
                is_stale = age.total_seconds() > SCRAPER_STALE_MINUTES * 60
            except ValueError:
                is_stale = True
        else:
            is_stale = True
        if not is_stale:
            return jsonify({"error": "Scraper is already running"}), 409

    data = request.get_json() or {}
    gw   = int(data.get('gw', 1))
    match_ids = data.get('match_ids') or None
    if match_ids is not None:
        try:
            match_ids = [int(m) for m in match_ids]
        except (TypeError, ValueError):
            return jsonify({"error": "match_ids must be a list of integers"}), 400
        if not match_ids:
            return jsonify({"error": "match_ids was empty — select at least one match"}), 400
    start_scrape(gw, trigger='manual', match_ids=match_ids)

    return jsonify({"status": "started", "gw": gw})


REQUIRED_PLAYER_FIELDS = set(scraper_lib.empty_player("", "").keys())


@app.route('/api/scrape/upload', methods=['POST'])
def upload_scrape():
    """
    Lets a logged-in manager submit stats scraped on their own machine
    (via scripts/scrape_and_upload.py) straight into the live DB, without
    needing Render access -- a stopgap for whenever the hosted scraper
    itself can't reach WhoScored (see scripts/diagnose_scrape.py).
    Same login-required gate as every other write endpoint (see
    require_login_for_writes) -- any logged-in manager can use this, same
    as "Press the Button" isn't restricted to a specific manager either.

    Reuses scraper.py's own save_to_db() -- by default idempotent (skips
    players already saved for this match_id), same as a normal server-side
    scrape. Pass "rescrape": true to instead delete and replace that
    match's existing rows -- needed when an earlier scrape (e.g. someone
    running this mid-match) already wrote incomplete/stale stats that a
    later, complete scrape would otherwise silently skip forever.
    """
    data = request.get_json(silent=True) or {}

    try:
        match_id = int(data.get('match_id'))
        gw_number = int(data.get('gw'))
        goals_home = int(data.get('goals_home'))
        goals_away = int(data.get('goals_away'))
    except (TypeError, ValueError):
        return jsonify({"error": "match_id, gw, goals_home, goals_away must all be integers"}), 400

    rescrape = bool(data.get('rescrape'))

    home_team = (data.get('home_team') or '').strip()
    away_team = (data.get('away_team') or '').strip()
    if not home_team or not away_team:
        return jsonify({"error": "home_team and away_team are required"}), 400

    players = data.get('players')
    if not isinstance(players, list) or not players:
        return jsonify({"error": "players must be a non-empty list"}), 400
    for p in players:
        if not isinstance(p, dict) or not REQUIRED_PLAYER_FIELDS.issubset(p.keys()):
            missing = REQUIRED_PLAYER_FIELDS - set(p.keys() if isinstance(p, dict) else [])
            return jsonify({"error": f"player entry missing field(s): {sorted(missing)}"}), 400

    conn = get_db()
    before = conn.execute("SELECT COUNT(*) FROM raw_stats WHERE match_id=?", (match_id,)).fetchone()[0]
    conn.close()

    scraper_lib.save_to_db(players, match_id, gw_number, home_team, away_team, goals_home, goals_away, rescrape=rescrape)

    conn = get_db()
    after = conn.execute("SELECT COUNT(*) FROM raw_stats WHERE match_id=?", (match_id,)).fetchone()[0]
    action = 'manual_rescrape' if rescrape else 'manual_upload'
    verb = 'Replaced' if rescrape else 'Uploaded'
    log_audit(conn, current_manager_id(), 'scraper', action,
              f"{verb} {after} player row(s) for match {match_id} (GW{gw_number}, "
              f"{home_team} {goals_home}-{goals_away} {away_team}) from a local scrape.",
              {"match_id": match_id, "gw": gw_number, "players_submitted": len(players),
               "rows_inserted": after - before, "rescrape": rescrape})
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok", "match_id": match_id, "gw": gw_number,
        "players_submitted": len(players), "rows_inserted": after - before, "rescrape": rescrape,
    })


@app.route('/api/scrape/status')
def scrape_status():
    return jsonify(read_scraper_status())


@app.route('/api/scraper/force-reset', methods=['POST'])
def scraper_force_reset():
    write_scraper_status({"status": "idle", "started_at": None, "completed_at": None, "gw": None})
    return jsonify({"status": "ok"})


@app.route('/api/scraper/cancel', methods=['POST'])
def scraper_cancel():
    """
    Actually kills the running scrape, unlike force-reset (which only
    clears the status file and leaves the process running underneath —
    see force-reset above). Signals the whole process group so
    scrape_gw.py's Playwright/Chromium child dies too, not just the
    Python parent.
    """
    status = read_scraper_status()
    if status.get('status') != 'running':
        return jsonify({"error": "No scrape is currently running"}), 409
    pid = status.get('pid')
    if not pid:
        return jsonify({"error": "No process recorded for this run — try force-reset instead"}), 409
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already dead — the background thread just hasn't finished
        # writing the final status yet. Nothing left to kill.
        pass
    return jsonify({"status": "cancelling"})


# ── Auto-scrape poller ──────────────────────────────────────────────────────
# Fires the scraper automatically 30 min before a gameweek's lineup lock (i.e.
# 5.5h after its last kickoff — the lock itself is 6h after), so the final
# version of the stats is captured before the lock freezes lineups. Doesn't
# replace the manual "Press the Button" flow, which stays available for the
# 1-3 day post-lock correction window — this only ever fires once per gw.
AUTO_SCRAPE_POLL_SECONDS = 300  # 5 minutes
AUTO_SCRAPE_LEAD = timedelta(hours=5.5)


def maybe_auto_scrape():
    """One poll pass: fire an auto-scrape for the first due gw found, if any.
    Only one gw per pass — if several are somehow due at once (e.g. the app
    was down for a while), the next pass picks up the next one rather than
    stacking concurrent scrapes."""
    if read_scraper_status().get('status') == 'running':
        return

    conn = get_db()
    try:
        gws = [r[0] for r in conn.execute(
            "SELECT gw_number FROM gameweeks WHERE season=? ORDER BY gw_number", (DRAFT_SEASON,)
        ).fetchall()]

        for gw in gws:
            _, latest_kickoff = get_gw_kickoff_bounds(conn, DRAFT_SEASON, gw)
            if latest_kickoff is None:
                continue  # no schedule yet for this gw — nothing to trigger off of
            if now_eastern_naive() < latest_kickoff + AUTO_SCRAPE_LEAD:
                continue  # not due yet

            already_auto_run = conn.execute(
                "SELECT 1 FROM scraper_runs WHERE gw_start=? AND trigger='auto'", (gw,)
            ).fetchone()
            if already_auto_run:
                continue  # this gw already got its one auto-fire

            print(f"[auto-scrape] GW{gw} is due (5.5h past last kickoff) — starting scrape.")
            start_scrape(gw, trigger='auto')
            return  # one at a time; re-check the rest on the next pass
    finally:
        conn.close()


def auto_scrape_loop():
    while True:
        try:
            maybe_auto_scrape()
        except Exception as e:
            print(f"[auto-scrape poller] error: {e}")
        time.sleep(AUTO_SCRAPE_POLL_SECONDS)


@app.route('/internal/auto-scrape-trigger', methods=['POST'])
def auto_scrape_trigger():
    """
    Hosted-deploy equivalent of auto_scrape_loop() above. gunicorn (the
    production server) never runs the `if __name__ == '__main__':` block
    below, so the in-process poller thread only ever starts for local
    `python app.py` runs — in production this route is what a host-level
    scheduled job (e.g. a Render Cron Job hitting this on a 5-minute
    schedule) calls instead, running the exact same maybe_auto_scrape()
    check-and-fire logic. Guarded by a shared-secret header so it can't be
    triggered by anyone who just finds the URL.
    """
    secret = os.environ.get('INTERNAL_TRIGGER_SECRET')
    if not secret or request.headers.get('X-Trigger-Secret') != secret:
        return jsonify({"error": "unauthorized"}), 403
    try:
        maybe_auto_scrape()
        return jsonify({"status": "checked"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── World Cup draft-randomizer ──────────────────────────────────────────────
# See working instructions/draft_randomizer_world_cup_requirements.md. The
# generator itself is stateless (no DB writes) — the client animates a result
# it fetched, then optionally "locks it in" via a short-lived server-side
# token so the persisted draft_order always matches exactly what was watched,
# without trusting a client-submitted order.

WORLD_CUP_EMOJI_POOL = ["⚽", "🦁", "🐺", "🦅", "🐍", "🦊", "🐢", "🐙",
                         "🦈", "🐯", "🐸", "🦉", "🐝", "🦍", "🐳", "🥷"]

WORLD_CUP_PENDING_RESULTS = {}  # token -> {"draft_order": [...], "created_at": float}
WORLD_CUP_TOKEN_TTL_SECONDS = 2 * 60 * 60


@app.route('/draft-randomizer-poc')
def draft_randomizer_poc():
    conn = get_db()
    managers = conn.execute("SELECT id, name, team_name FROM managers ORDER BY name").fetchall()
    conn.close()
    if len(managers) != world_cup_sim.NUM_PLAYERS:
        return f"World Cup POC requires exactly {world_cup_sim.NUM_PLAYERS} managers.", 400
    manager_names = {m['id']: m['name'] for m in managers}
    manager_teams = {m['id']: m['team_name'] for m in managers}
    return render_template('draft_randomizer.html', managers=managers,
                            manager_names=manager_names, manager_teams=manager_teams,
                            emoji_pool=WORLD_CUP_EMOJI_POOL)


@app.route('/api/world-cup-sim/generate', methods=['POST'])
def world_cup_sim_generate():
    data = request.get_json(silent=True) or {}
    avatars = data.get('avatars') or {}

    if len(avatars) != world_cup_sim.NUM_PLAYERS:
        return jsonify({"error": f"Expected exactly {world_cup_sim.NUM_PLAYERS} avatar picks."}), 400
    if len(set(avatars.values())) != len(avatars):
        return jsonify({"error": "Avatar choices must be unique."}), 400
    if not all(v in WORLD_CUP_EMOJI_POOL for v in avatars.values()):
        return jsonify({"error": "Unknown avatar emoji."}), 400

    conn = get_db()
    valid_ids = {row['id'] for row in conn.execute("SELECT id FROM managers").fetchall()}
    conn.close()
    try:
        manager_ids = [int(mid) for mid in avatars.keys()]
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid manager id."}), 400
    if not set(manager_ids).issubset(valid_ids):
        return jsonify({"error": "Unknown manager id."}), 400

    avatars_by_id = {int(mid): emoji for mid, emoji in avatars.items()}
    result = world_cup_sim.generate_result(manager_ids=manager_ids, avatars=avatars_by_id)

    cutoff = time.time() - WORLD_CUP_TOKEN_TTL_SECONDS
    for stale_token in [t for t, v in WORLD_CUP_PENDING_RESULTS.items() if v['created_at'] < cutoff]:
        del WORLD_CUP_PENDING_RESULTS[stale_token]

    token = uuid.uuid4().hex
    WORLD_CUP_PENDING_RESULTS[token] = {"draft_order": result["draft_order"], "created_at": time.time()}
    result["token"] = token
    return jsonify(result)


@app.route('/api/world-cup-sim/lock-in', methods=['POST'])
def world_cup_sim_lock_in():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    pending = WORLD_CUP_PENDING_RESULTS.pop(token, None) if token else None
    if not pending:
        return jsonify({"error": "This result has expired or was already used — run the simulation again."}), 410

    conn = get_db()
    state = get_draft_state(conn)
    if state['status'] not in ('not_started', 'randomizing'):
        conn.close()
        return jsonify({"error": "Draft order is already locked in."}), 409

    conn.execute("DELETE FROM draft_order WHERE season=?", (DRAFT_SEASON,))
    for i, manager_id in enumerate(pending["draft_order"], start=1):
        conn.execute(
            "INSERT INTO draft_order (season, pick_slot, manager_id) VALUES (?, ?, ?)",
            (DRAFT_SEASON, i, manager_id)
        )
    conn.execute(
        "UPDATE draft_state SET status='ready', spin_count=3 WHERE season=?",
        (DRAFT_SEASON,)
    )
    log_audit(conn, session.get('manager_id'), 'draft', 'lock_in',
              "Locked in draft order via World Cup randomizer")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/healthz')
def healthz():
    # No DB/scraper checks here on purpose — Render hits this every few
    # seconds and a slow query or a scrape in progress shouldn't make it
    # think the whole service is down and restart it.
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    # Only matters when running `python app.py` directly (local dev, or a
    # manual run on a host) — gunicorn (how this is actually served once
    # deployed) imports the `app` object and never executes this block at
    # all, so it never inherits this default. Still: never set
    # FLASK_DEBUG=true anywhere public-facing, since debug mode exposes an
    # interactive debugger/stack traces to anyone who can reach the app.
    DEBUG = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    # Guard against Flask's debug-mode reloader starting two competing
    # pollers: app.run(debug=True) re-execs this same script as a child
    # worker process with WERKZEUG_RUN_MAIN set, while the original process
    # becomes a file-watching monitor that never actually serves requests —
    # only start the poller in the real worker (or immediately if DEBUG is
    # ever turned off, since then there's no reloader/forking at all).
    if not DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        poller = threading.Thread(target=auto_scrape_loop, daemon=True)
        poller.start()
    app.run(debug=DEBUG, port=int(os.environ.get('PORT', 5000)))
