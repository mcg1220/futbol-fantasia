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

DB_PATH             = os.path.join(os.path.dirname(__file__), 'data', 'fantasia.db')
SCRAPER_STATUS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'scraper_status.json')
SCRIPTS_DIR         = os.path.join(os.path.dirname(__file__), 'scripts')
BADGES_PATH         = os.path.join(os.path.dirname(__file__), 'data', 'badges.json')
PHOTOS_DIR          = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'team_photos')
ALLOWED_PHOTO_EXTS  = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MEMES_DIR           = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'memes')
ALLOWED_MEME_EXTS   = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

sys.path.insert(0, SCRIPTS_DIR)
from scoring_engine import calc_player_score, get_scoring_config, calc_bulk_season_totals, get_team_goals_conceded

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

LOGIN_EXEMPT_PATHS_EXACT = {'/login', '/internal/auto-scrape-trigger'}


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
        return {'current_manager_name': None, 'current_manager_id': None}
    conn = get_db()
    row = conn.execute("SELECT name FROM managers WHERE id=?", (manager_id,)).fetchone()
    conn.close()
    return {'current_manager_name': row['name'] if row else None, 'current_manager_id': manager_id}


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
    row = conn.execute("""
        SELECT MAX(g.gw_number)
        FROM results r
        JOIN gameweeks g ON g.id = r.gw_id
        WHERE g.season = ?
    """, (season,)).fetchone()[0]
    return row if row else 1


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
    """club -> {'opponent', 'date', 'time'} for a single gameweek. `date`/`time`
    are the formatted strings from format_kickoff (None if not yet scheduled)."""
    rows = conn.execute("""
        SELECT f.home_club, f.away_club, f.match_date, f.kickoff_time
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ?
    """, (gw_number, season)).fetchall()

    info = {}
    for r in rows:
        date_label, time_label = format_kickoff(r['match_date'], r['kickoff_time'])
        for club, opponent in ((r['home_club'], r['away_club']), (r['away_club'], r['home_club'])):
            info[club] = {'opponent': opponent, 'date': date_label, 'time': time_label}
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
            'home_club': r['home_club'], 'away_club': r['away_club'], 'time': time_label
        })

    if unscheduled:
        groups.append({'label': None, 'matches': [
            {'home_club': r['home_club'], 'away_club': r['away_club'], 'time': None}
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
        "SELECT id, name, team_name FROM managers ORDER BY name"
    ).fetchall()

    glance = {}
    for row in glance_rows:
        glance.setdefault(row['gw_number'], {})[row['manager_id']] = row

    conn.close()

    return render_template('standings.html',
        standings=standings_rows,
        managers=managers,
        gws=list(range(1, 34)),
        glance=glance,
        season=season,
        scraper_status=read_scraper_status(),
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
        row = c.execute("""
            SELECT MAX(g.gw_number)
            FROM results r
            JOIN gameweeks g ON g.id = r.gw_id
            WHERE g.season = ?
        """, (season,)).fetchone()[0]
        gw = row if row else 1

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

    fixture_rows = c.execute("""
        SELECT f.home_club, f.away_club, f.match_date, f.kickoff_time
        FROM fixtures f
        JOIN gameweeks g ON g.id = f.gw_id
        WHERE g.gw_number = ? AND f.season = ?
        ORDER BY f.home_club
    """, (gw, season)).fetchall()

    day_groups = build_fixture_day_groups(fixture_rows)

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

    row = c.execute("""
        SELECT MAX(g.gw_number)
        FROM results r
        JOIN gameweeks g ON g.id = r.gw_id
        WHERE g.season = ?
    """, (season,)).fetchone()[0]
    current_gw = row if row else 1

    matchup = c.execute("""
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
    """, (manager_id,)*3 + (manager_id, manager_id, season, current_gw,
                             manager_id, manager_id)).fetchone()

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

    club_map = {}
    if player_names:
        ph = ','.join('?' * len(player_names))
        club_rows = c.execute(f"""
            SELECT player_name, club
            FROM raw_stats
            WHERE player_name IN ({ph})
            GROUP BY player_name
            HAVING MAX(gw_number)
        """, player_names).fetchall()
        club_map = {r['player_name']: r['club'] for r in club_rows}

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
        pos   = (player['position_slot'] or 'MID').upper()
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
        stats_map = {r['player_name']: r for r in stats_rows}

    POS_ORDER = {'FW': 0, 'MID': 1, 'DEF': 2, 'GK': 3}

    def pos_sort(r):
        return POS_ORDER.get((r['position_slot'] or '').upper(), 9)

    starters = sorted([r for r in roster_rows if r['slot_type'] == 'starter'], key=pos_sort)
    bench    = [r for r in roster_rows if r['slot_type'] == 'bench']
    ir       = [r for r in roster_rows if r['slot_type'] == 'ir']

    position_check = check_position_counts(conn, manager_id, current_gw)

    gw_locked  = is_gw_locked(conn, season, current_gw)
    locked_map = {
        r['player_name']: is_player_locked(conn, season, current_gw, club_map.get(r['player_name']))
        for r in roster_rows
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
        badges=badges,
        season=season,
        scraper_status=read_scraper_status(),
        position_check=position_check,
        gw_locked=gw_locked,
        locked_map=locked_map,
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
    )


@app.route('/api/roster/update', methods=['POST'])
def update_roster_slot():
    """
    Move a player between slots (starter position / bench / IR) for a given
    gameweek — the current one from the main Team page, or a future one from
    the Plan Future Lineup panel. Follows the same open-ended roster range
    model as the draft and Player Add/Drop's swap endpoint (gw_start <= gw <=
    gw_end-or-NULL), but — unlike the old version of this endpoint — SPLITS
    the range at `gw` instead of editing the covering row in place, so a
    change made for one gw can't silently rewrite the slot/position that was
    actually live in other gws. Mirrors execute_roster_swap's split pattern.
    The dropdown UI already restricts options to a player's real eligibility,
    so no server-side eligibility rejection is needed here — but locking
    (Part 3) is enforced below.
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
    c = conn.cursor()

    row = c.execute("""
        SELECT id, slot_type, position_slot, gw_start, gw_end FROM rosters
        WHERE manager_id=? AND player_name=?
          AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
    """, (manager_id, player_name, gw, gw)).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Roster row not found for this player/GW"}), 404

    club_row = c.execute("SELECT club FROM players WHERE name=?", (player_name,)).fetchone()
    club = club_row['club'] if club_row else None

    locked, reason = is_change_locked(conn, DRAFT_SEASON, gw, club, row['slot_type'], new_slot_type)
    if locked:
        conn.close()
        return jsonify({"error": reason}), 403

    # Roster shape is fixed: exactly 15 non-IR (starter + bench) plus at most
    # 1 IR slot — IR is a single reserved spot, not extra bench space, so
    # both directions of crossing that boundary need checking here.
    if new_slot_type == 'ir' and row['slot_type'] != 'ir':
        existing_ir = c.execute("""
            SELECT 1 FROM rosters
            WHERE manager_id=? AND slot_type='ir'
              AND gw_start<=? AND (gw_end IS NULL OR gw_end>=?)
        """, (manager_id, gw, gw)).fetchone()
        if existing_ir:
            conn.close()
            return jsonify({"error": "Only one player can be on IR at a time."}), 409
    elif row['slot_type'] == 'ir' and new_slot_type != 'ir':
        if count_active_roster_slots(conn, manager_id, gw) >= PLAYER_PICKS_PER_TEAM:
            conn.close()
            return jsonify({"error": "Your active roster is already full (15) — drop a player before moving this one off IR."}), 409

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

    log_audit(conn, manager_id, 'roster', 'lineup_change',
              f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Moved {player_name}: {row['slot_type']} ({row['position_slot']}) → {new_slot_type} ({new_position_slot})",
              {"player": player_name, "gw": gw, "from": dict(row), "to": {"slot_type": new_slot_type, "position_slot": new_position_slot}})
    conn.commit()

    counts = c.execute("""
        SELECT position_slot, COUNT(*) as cnt
        FROM rosters
        WHERE manager_id=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?) AND slot_type='starter'
        GROUP BY position_slot
    """, (manager_id, gw, gw)).fetchall()

    conn.close()

    formation = {r['position_slot']: r['cnt'] for r in counts}
    return jsonify({"status": "ok", "formation": formation})


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
    """Fresh current roster for a manager — used by the Add/Claim drop-player
    picker so it never shows a stale list from an earlier page load."""
    conn = get_db()
    season = DRAFT_SEASON
    current_gw = get_current_gw(conn, season)
    rows = conn.execute("""
        SELECT player_name, slot_type, position_slot
        FROM rosters
        WHERE manager_id=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
        ORDER BY slot_type, position_slot
    """, (manager_id, current_gw, current_gw)).fetchall()
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

    totals_2025 = calc_bulk_season_totals(conn, '2025-26', match_id_filter=(0, SEASON_CUTOFF))
    totals_2026 = calc_bulk_season_totals(conn, '2026-27', match_id_filter=(SEASON_CUTOFF, 9_999_999))

    elig_rows = c.execute("""
        SELECT p.name, pe.position FROM players p
        JOIN player_eligibility pe ON pe.player_id = p.id
    """).fetchall()
    eligibility_by_player = {}
    for r in elig_rows:
        eligibility_by_player.setdefault(r['name'], []).append(r['position'])

    # Players with no club (or at a relegated club) aren't in the current PL
    # player pool — exclude from browse, but their historical stats remain.
    browse_rows = c.execute(f"""
        SELECT p.name, p.club, p.position
        FROM players p
        WHERE p.club IS NOT NULL AND p.club != ''
          AND p.club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
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

    waiver_windows_rows = c.execute(
        "SELECT * FROM waiver_windows WHERE season=? ORDER BY window_number DESC", (season,)
    ).fetchall()
    waiver_results = []
    for w in waiver_windows_rows:
        claims = c.execute("""
            SELECT wc.*, m.name AS manager_name, m.team_name
            FROM waiver_claims wc JOIN managers m ON m.id = wc.manager_id
            WHERE wc.window_id=? ORDER BY wc.sequence_number
        """, (w['id'],)).fetchall()
        waiver_results.append({**dict(w), 'claims': [dict(cl) for cl in claims]})

    my_shortlist = get_my_shortlist(conn)
    conn.close()
    return render_template('history.html',
        season=season,
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
        waiver_results=waiver_results,
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


def execute_roster_swap(conn, manager_id, add_player, drop_player, gw, source):
    """
    Core roster swap: drop `drop_player` from manager_id's roster and add
    `add_player` in their place. If the incoming player is eligible for the
    outgoing player's exact slot, they inherit it; otherwise they land on the
    bench. Follows the open-ended roster range model (gw_start=gw, gw_end=NULL).

    `drop_player` may be None/falsy — a pure add, only allowed when the
    manager has an open non-IR roster spot (see count_active_roster_slots).
    The incoming player always lands on the bench in that case, since
    there's no outgoing slot to inherit.

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

    if drop_row and drop_row['position_slot'] in eligible:
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

    c.execute("""
        INSERT INTO transactions (manager_id, added_player, dropped_player, source, gw, season, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (manager_id, add_player, drop_player, source, gw, DRAFT_SEASON, now_eastern_naive().isoformat()))

    # Transfer-draft picks log their own richer entry (round/draft type) at
    # the call site, since a pass there never reaches this function at all —
    # skip here to avoid a duplicate, less-detailed entry for the same pick.
    if not source.endswith('_transfer_draft'):
        action = 'waiver_claim' if source == 'waiver_claim' else 'add_drop'
        summary = f"{gw_change_label(conn, DRAFT_SEASON, gw)} — Added {add_player}"
        summary += f", dropped {drop_player}" if drop_player else " (no drop — had an open roster spot)"
        log_audit(conn, manager_id, 'roster', action, summary,
                  {"added": add_player, "dropped": drop_player, "gw": gw, "source": source})

    return True, {"slot_type": new_slot_type, "position_slot": new_position_slot}


@app.route('/api/roster/swap', methods=['POST'])
def swap_roster_player():
    data = request.get_json() or {}
    manager_id  = current_manager_id()
    drop_player = data.get('drop_player') or None
    add_player  = data.get('add_player')
    gw          = data.get('gw')

    if not add_player or not gw:
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db()
    ok, info = execute_roster_swap(conn, manager_id, add_player, drop_player, gw, 'roster_swap')
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


def get_waiver_order(conn, season):
    rows = conn.execute("""
        SELECT wo.manager_id, wo.position, m.name, m.team_name
        FROM waiver_order wo JOIN managers m ON m.id = wo.manager_id
        WHERE wo.season=? ORDER BY wo.position
    """, (season,)).fetchall()
    return [dict(r) for r in rows]


def seed_waiver_order_if_needed(conn, season):
    """Initial waiver order = reverse draft order, per spec. Seeded once,
    lazily, the first time a window is opened."""
    existing = conn.execute("SELECT 1 FROM waiver_order WHERE season=? LIMIT 1", (season,)).fetchone()
    if existing:
        return
    draft_rows = conn.execute(
        "SELECT manager_id FROM draft_order WHERE season=? ORDER BY pick_slot DESC", (season,)
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
        log_audit(conn, None, 'waiver', 'open_window', f"Opened waiver window #{next_num} (GW{gw})")
        conn.commit()
        return jsonify({"status": "ok", "window_number": next_num, "gw": gw})
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

        owned = conn.execute("""
            SELECT 1 FROM rosters
            WHERE player_name=? AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
        """, (add_player, gw, gw)).fetchone()
        if owned:
            conn.execute("ROLLBACK")
            return jsonify({"error": f"{add_player} is already owned"}), 409

        if drop_player:
            drop_row = conn.execute("""
                SELECT id FROM rosters WHERE manager_id=? AND player_name=?
                  AND gw_start <= ? AND (gw_end IS NULL OR gw_end >= ?)
            """, (manager_id, drop_player, gw, gw)).fetchone()
            if not drop_row:
                conn.execute("ROLLBACK")
                return jsonify({"error": f"{drop_player} is not on this roster"}), 404
        elif count_active_roster_slots(conn, manager_id, gw) >= PLAYER_PICKS_PER_TEAM:
            conn.execute("ROLLBACK")
            return jsonify({"error": "Your roster is full — drop a player to submit this claim (or free up a spot by moving someone to IR)."}), 409

        next_priority = conn.execute("""
            SELECT COALESCE(MAX(priority), 0) + 1 FROM waiver_claims
            WHERE window_id=? AND manager_id=? AND status='pending'
        """, (window['id'], manager_id)).fetchone()[0]

        conn.execute("""
            INSERT INTO waiver_claims (window_id, manager_id, add_player, drop_player, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (window['id'], manager_id, add_player, drop_player, next_priority, now_eastern_naive().isoformat()))
        claim_summary = f"Submitted waiver claim: add {add_player}"
        claim_summary += f", drop {drop_player}" if drop_player else " (no drop — had an open roster spot)"
        log_audit(conn, manager_id, 'waiver', 'claim_submitted', claim_summary,
                  {"add_player": add_player, "drop_player": drop_player, "gw": gw})
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

            ok, info = execute_roster_swap(conn, next_manager, claim['add_player'], claim['drop_player'], gw, 'waiver_claim')
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

    browse_players = []
    if state['status'] in ('ready', 'in_progress', 'complete'):
        owner_map = get_owner_map(conn, DRAFT_SEASON, 1)
        totals_2025, eligibility_by_player, stat_sums, projections = compute_full_player_stats(conn)

        rows = c.execute(f"""
            SELECT p.name, p.club FROM players p
            WHERE p.club IS NOT NULL AND p.club != ''
              AND p.club NOT IN ({','.join('?' * len(RELEGATED_CLUBS))})
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


def start_scrape(gw, trigger='manual'):
    """Shared by the manual 'Press the Button' route and the auto-scrape
    poller — launches the subprocess, writes the running status (with its
    pid, atomically — see run_scraper_background's docstring for why),
    then hands the already-running proc off to a background thread to
    stream and report on. Caller is responsible for checking the scraper
    isn't already running."""
    proc = subprocess.Popen(
        [sys.executable, 'scrape_gw.py', '--gw', str(gw), '--season', '2026-27'],
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
    start_scrape(gw, trigger='manual')

    return jsonify({"status": "started", "gw": gw})


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
