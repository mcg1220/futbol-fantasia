"""
Keeps the `players` table in step with what the scraper actually sees, so
anyone who plays in a scraped match becomes pickable in Add/Drop without a
manual fix. Called from scraper.save_to_db() (both the local upload path and
a server-side scrape), inside the same transaction as the raw_stats insert.

Players are matched on their WhoScored ID (read off the match page's lineup
link -- authoritative), then on exact name. Names always follow WhoScored's
spelling (standing rule, accents included).

  * New player (no row by ID or name, nothing similar)  -> added: club from
    the match, position from the match-page lineup slot, draftable. The
    uploader then refines positions from his WhoScored profile
    (/api/players/positions).
  * Same ID, accent-only spelling change on WhoScored   -> renamed everywhere
    (raw_stats included) to the new spelling, so his stats stay joined.
  * Soft-excluded (draftable=0) but now in a matchday squad -> made pickable.
  * Moved clubs (or still listed on a relegated club)   -> club updated.
  * Anything ambiguous -- a different-looking name under a known ID, a
    similar name with no ID to confirm it, a name clash between two IDs --
    is NOT changed, only reported (flags), for a person to look at.
"""
import re
import unicodedata

RELEGATED_CLUBS = ('Wolves', 'West Ham', 'Burnley')  # keep in sync with app.py

MATCH_POSITION = {
    'GK': 'GK',
    'DC': 'DEF', 'DR': 'DEF', 'DL': 'DEF',
    'DMC': 'MID', 'DML': 'MID', 'DMR': 'MID', 'MC': 'MID', 'MR': 'MID', 'ML': 'MID',
    'AMC': 'MID', 'AMR': 'MID', 'AML': 'MID',
    'FW': 'FW',
}
SOURCE_MATCH = 'whoscored_match'      # position guessed from one lineup slot
SOURCE_PROFILE = 'whoscored_live'     # positions from the WhoScored profile

NAME_COLUMNS = {'player_name', 'add_player', 'drop_player', 'added_player', 'dropped_player'}
# Tables where a rename colliding with an existing row for the new spelling
# should just drop the old-spelling row (the survivor's wins).
DEDUPE_TABLES = {'player_projections', 'player_start_status', 'shortlists',
                 'transfer_pool', 'ir_eligibility_overrides'}


def norm(s):
    d = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in d if not unicodedata.combining(c)).lower()
    return re.sub(r'\s+', ' ', s.replace('ı', 'i').replace('ø', 'o').replace('ł', 'l')).strip()


def _name_columns(conn):
    out = []
    # fetchall(): `conn` may be a cursor, and a nested execute on the same
    # cursor would silently end the outer loop after the first table.
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for (table,) in tables:
        if table == 'players':
            continue
        for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
            if col[1] in NAME_COLUMNS:
                out.append((table, col[1]))
    return out


def _rename_everywhere(conn, old, new):
    """Rename a player in every table that stores a player name, raw_stats
    included, then players.name. Raises (caller rolls back) on a clash it
    can't safely resolve."""
    for table, col in _name_columns(conn):
        conn.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old))
        left = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (old,)).fetchone()[0]
        if left:
            if table in DEDUPE_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE {col}=?", (old,))
            else:
                raise RuntimeError(f"{table}.{col}: {left} row(s) for {old!r} clash with {new!r}")
    conn.execute("UPDATE players SET name=? WHERE name=?", (new, old))


def sync_players(conn, players):
    """`players` is the scraped list (player_name, club, player_id, position
    code). Returns a report dict; never raises -- a failure on one player is
    rolled back to a savepoint and reported as a flag."""
    rep = {'added': [], 'renamed': [], 'made_pickable': [], 'club_updated': [], 'flags': []}
    cur = conn.cursor()
    for p in players:
        name, club, wsid = p.get('player_name'), p.get('club'), p.get('player_id')
        if not name or not club or not wsid:
            continue
        cur.execute("SAVEPOINT sync_player")
        try:
            _sync_one(cur, rep, name, club, int(wsid), MATCH_POSITION.get(p.get('position') or ''))
            cur.execute("RELEASE sync_player")
        except Exception as e:
            cur.execute("ROLLBACK TO sync_player")
            cur.execute("RELEASE sync_player")
            rep['flags'].append(f"{name} (WhoScored {wsid}): couldn't sync, left as is -- {e}")
    return rep


def _row(cur, sql, args):
    r = cur.execute(sql, args).fetchone()
    if r is None:
        return None
    return dict(zip([d[0] for d in cur.description], r))


def _sync_one(cur, rep, name, club, wsid, match_pos):
    by_id = _row(cur, "SELECT * FROM players WHERE whoscored_id=?", (wsid,))
    by_name = _row(cur, "SELECT * FROM players WHERE name=?", (name,))

    if by_id and by_id['name'] != name:
        if by_name:
            rep['flags'].append(f"{name!r} (WhoScored {wsid}): that ID belongs to {by_id['name']!r} "
                                f"but {name!r} also has its own row -- two rows, left as is")
            return
        if norm(by_id['name']) != norm(name):
            rep['flags'].append(f"{name!r} (WhoScored {wsid}): that ID belongs to {by_id['name']!r} "
                                f"-- names differ, not renaming automatically")
            return
        _rename_everywhere(cur, by_id['name'], name)
        rep['renamed'].append(f"{by_id['name']} -> {name}")
        by_name = dict(by_id, name=name)

    if not by_name:
        _add_or_flag(cur, rep, name, club, wsid, match_pos)
        return

    # Existing row under this exact name.
    if by_name.get('whoscored_id') not in (None, wsid):
        rep['flags'].append(f"{name!r}: scraped with WhoScored {wsid} but the row has "
                            f"{by_name['whoscored_id']} -- two players with one name? left as is")
        return
    if by_name.get('whoscored_id') is None:
        cur.execute("UPDATE players SET whoscored_id=? WHERE id=?", (wsid, by_name['id']))
    if by_name.get('draftable') == 0 and (by_name.get('position') or match_pos):
        cur.execute("UPDATE players SET draftable=1 WHERE id=?", (by_name['id'],))
        rep['made_pickable'].append(name)
    if (by_name.get('club') or '') != club:
        cur.execute("UPDATE players SET club=? WHERE id=?", (club, by_name['id']))
        rep['club_updated'].append(f"{name}: {by_name.get('club') or 'no club'} -> {club}")
    if not cur.execute("SELECT 1 FROM player_eligibility WHERE player_id=?", (by_name['id'],)).fetchone():
        pos = by_name.get('position') or match_pos  # '' = auto-added sub, still pending
        if pos:
            cur.execute("UPDATE players SET position=? WHERE id=? AND COALESCE(position, '')=''",
                        (pos, by_name['id']))
            cur.execute("INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                        (by_name['id'], pos, SOURCE_MATCH))


def _add_or_flag(cur, rep, name, club, wsid, match_pos):
    # Similar existing names: accent-insensitive match, or a one-word name
    # that's the start of a longer one ("Allan" / "Allan Elias").
    n = norm(name)
    similar = []
    for (other,) in cur.execute("SELECT name FROM players").fetchall():
        o = norm(other)
        if o == n or (' ' not in n and o.split(' ')[0] == n) or (' ' not in o and n.split(' ')[0] == o):
            similar.append(other)
    if similar:
        rep['flags'].append(f"{name!r} ({club}, WhoScored {wsid}): no row, but similar to {similar} "
                            f"-- not added, needs a look")
        return
    pos = match_pos
    # Sub-only appearance -> no lineup slot to go on. players.position is NOT
    # NULL, so store '' and keep him out of the pool (draftable=0, no
    # eligibility) until set_profile_positions() fills it in.
    cur.execute("INSERT INTO players (name, club, position, whoscored_id, draftable) VALUES (?,?,?,?,?)",
                (name, club, pos or '', wsid, 1 if pos else 0))
    pid = cur.lastrowid
    if pos:
        cur.execute("INSERT INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                    (pid, pos, SOURCE_MATCH))
    rep['added'].append({'name': name, 'club': club, 'whoscored_id': wsid, 'position': pos})


def set_profile_positions(conn, wsid, positions):
    """Replace an auto-added player's match-guessed positions with the ones on
    his WhoScored profile (first = primary) and make him pickable. Refuses to
    touch players whose positions came from anywhere else (Fantrax import,
    manual edits), so this can't overwrite curated eligibility."""
    positions = [p for p in positions if p in ('GK', 'DEF', 'MID', 'FW')]
    row = conn.execute("SELECT id, name FROM players WHERE whoscored_id=?", (wsid,)).fetchone()
    if not row or not positions:
        return None
    pid, name = row[0], row[1]
    sources = {r[0] for r in conn.execute("SELECT source FROM player_eligibility WHERE player_id=?", (pid,))}
    if sources - {SOURCE_MATCH}:
        return None
    conn.execute("DELETE FROM player_eligibility WHERE player_id=?", (pid,))
    for pos in positions:
        conn.execute("INSERT OR IGNORE INTO player_eligibility (player_id, position, source) VALUES (?,?,?)",
                     (pid, pos, SOURCE_PROFILE))
    conn.execute("UPDATE players SET position=?, draftable=1 WHERE id=?", (positions[0], pid))
    return name
