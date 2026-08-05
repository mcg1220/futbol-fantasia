// Shared game-log rendering, sorting, and opponent filtering.
// Used by Player Add/Drop, Team page, and Draft page — anywhere a
// per-player historical game-by-game table is shown.

const GAME_LOG_COLS = [
  {label: 'Season', key: 'season'},
  {label: 'GW', key: 'gw_number', num: true},
  {label: 'Club', key: 'club'},
  {label: 'Opp', key: 'opponent'},
  {label: 'FPts', key: 'fpts', num: true, sum: true, title: 'Fantasy Points'},
  {label: 'G', key: 'goals', num: true, sum: true, title: 'Goals'},
  {label: 'A', key: 'assists', num: true, sum: true, title: 'Assists'},
  {label: 'SOT', key: 'shots_on_target', num: true, sum: true, title: 'Shots on Target'},
  {label: 'KP', key: 'key_passes', num: true, sum: true, title: 'Key Passes'},
  {label: 'Drb', key: 'dribbles', num: true, sum: true, title: 'Dribbles'},
  {label: 'Tkl', key: 'tackles', num: true, sum: true, title: 'Tackles'},
  {label: 'Int', key: 'interceptions', num: true, sum: true, title: 'Interceptions'},
  {label: 'Clr', key: 'clearances', num: true, sum: true, title: 'Clearances'},
  {label: 'BlkSh', key: 'blocked_shots', num: true, sum: true, title: 'Blocked Shots'},
  {label: 'Crs', key: 'acc_crosses', num: true, sum: true, title: 'Accurate Crosses'},
  {label: 'LB', key: 'acc_long_balls', num: true, sum: true, title: 'Accurate Long Balls'},
  {label: 'Saves', key: 'saves', num: true, sum: true, title: 'Saves'},
  {label: 'PKSv', key: 'pk_saves', num: true, sum: true, title: 'Penalty Saves'},
  {label: 'GLC', key: 'glc', num: true, sum: true, title: 'Goal Line Clearance'},
  {label: 'LMT', key: 'lmt', num: true, sum: true, title: 'Last Man Tackle'},
  {label: 'ELG', key: 'elg', num: true, sum: true, title: 'Error Leading to Goal'},
  {label: 'OG', key: 'own_goals', num: true, sum: true, title: 'Own Goals'},
  {label: 'MOTM', key: 'motm', num: true, sum: true, title: 'Man of the Match'},
  {label: 'YC', key: 'yellow_cards', num: true, sum: true, title: 'Yellow Cards'},
  {label: 'RC', key: 'red_cards', num: true, sum: true, title: 'Red Cards'},
  {label: 'Mins', key: 'minutes_played', num: true, sum: true, title: 'Minutes Played'},
  {label: 'GC', key: 'goals_conceded', num: true, sum: true, title: 'Goals Conceded (while on the pitch)'},
  {label: 'CS', key: 'clean_sheet', num: true, sum: true, title: 'Clean Sheet (60+ mins played, 0 conceded)'},
];

function renderHistoryTable(allRows, hide2025, panelId, projection) {
  const rows = hide2025 ? allRows.filter(r => r.season !== '2025-26') : allRows;
  if (!rows.length && !projection) return '<div class="empty-state">No historical stats for this player.</div>';

  const tableId = `game-log-${panelId}`;
  return `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <input type="text" class="player-search-input" style="max-width:220px"
             placeholder="Filter by opponent..." oninput="filterGameLog('${tableId}', this.value)" />
    </div>
    <table class="game-log-table" id="${tableId}">
      <thead><tr>
        ${GAME_LOG_COLS.map((c, i) => `<th class="sortable" ${c.num ? 'data-type="num"' : ''} ${c.title ? `title="${c.title}"` : ''} onclick="sortGameLog('${tableId}', ${i})">${c.label}</th>`).join('')}
      </tr></thead>
      <tbody>${renderProjectedRow(projection)}${renderGameLogRows(rows)}</tbody>
      <tfoot><tr class="game-log-totals-row">${GAME_LOG_COLS.map(() => '<td></td>').join('')}</tr></tfoot>
    </table>
  `;
}

// Synthetic first row showing 2026-27 season projections (total-only — Fantrax
// doesn't give us a per-stat breakdown, so every stat column is a dash).
// Excluded from sorting/totaling/opponent-filtering — see sortGameLog,
// updateGameLogTotals, and filterGameLog.
function renderProjectedRow(projection) {
  if (!projection) return '';
  const title = `Projected average: ${projection.proj_avg.toFixed(2)} pts/GW`;
  return `
    <tr class="game-log-dummy-row">
      <td><span class="season-tag">Proj</span></td>
      <td>2026-27</td>
      <td>—</td>
      <td>—</td>
      <td class="game-log-pts" title="${title}">${projection.proj_total.toFixed(2)}</td>
      ${GAME_LOG_COLS.slice(5).map(() => '<td>—</td>').join('')}
    </tr>
  `;
}

function initGameLogSort(panelId) {
  const tableId = `game-log-${panelId}`;
  if (document.getElementById(tableId)) {
    sortGameLog(tableId, 1, 'desc');
    updateGameLogTotals(tableId);
  }
}

// Sums every `sum: true` column across whatever rows are currently visible
// (respects the opponent filter) — "Clean Sheet" totals as a count since
// each row is 0/1, which is exactly what you want to see (e.g. "9").
function updateGameLogTotals(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const footRow = table.querySelector('tfoot tr');
  if (!footRow) return;
  const visibleRows = Array.from(table.querySelectorAll('tbody tr:not(.game-log-dummy-row)')).filter(r => r.style.display !== 'none');
  const footCells = footRow.children;

  GAME_LOG_COLS.forEach((c, i) => {
    if (!c.sum) {
      footCells[i].textContent = i === 0 ? 'Totals' : '';
      return;
    }
    let total = 0;
    visibleRows.forEach(row => {
      const cell = row.children[i];
      const raw = cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent;
      const val = parseFloat(raw);
      if (!isNaN(val)) total += val;
    });
    footCells[i].textContent = c.key === 'fpts' ? total.toFixed(2) : total;
  });
}

function renderGameLogRows(rows) {
  return rows.map(r => `
    <tr data-opponent="${(r.opponent || '').toLowerCase()}">
      <td data-sort="${parseInt(r.season) * 100 + r.gw_number}"><span class="season-tag">${r.season}</span></td>
      <td data-sort="${parseInt(r.season) * 100 + r.gw_number}">${r.season} GW${r.gw_number}</td>
      <td>${r.club}</td>
      <td>${r.opponent || '—'}</td>
      <td class="game-log-pts">${r.fpts.toFixed(2)}</td>
      <td>${r.goals || 0}</td>
      <td>${r.assists || 0}</td>
      <td>${r.shots_on_target || 0}</td>
      <td>${r.key_passes || 0}</td>
      <td>${r.dribbles || 0}</td>
      <td>${r.tackles || 0}</td>
      <td>${r.interceptions || 0}</td>
      <td>${r.clearances || 0}</td>
      <td>${r.blocked_shots || 0}</td>
      <td>${r.acc_crosses || 0}</td>
      <td>${r.acc_long_balls || 0}</td>
      <td>${r.saves || 0}</td>
      <td>${r.pk_saves || 0}</td>
      <td>${r.glc || 0}</td>
      <td>${r.lmt || 0}</td>
      <td>${r.elg || 0}</td>
      <td>${r.own_goals || 0}</td>
      <td>${r.motm || 0}</td>
      <td>${r.yellow_cards || 0}</td>
      <td>${r.red_cards || 0}</td>
      <td>${r.minutes_played || 0}</td>
      <td data-sort="${r.goals_conceded != null ? r.goals_conceded : ''}">${r.goals_conceded != null ? r.goals_conceded : '—'}</td>
      <td data-sort="${r.clean_sheet || 0}">${r.clean_sheet ? '✓' : (r.clean_sheet == null ? '—' : '')}</td>
    </tr>
  `).join('');
}

function filterGameLog(tableId, query) {
  const q = query.toLowerCase();
  document.querySelectorAll(`#${tableId} tbody tr:not(.game-log-dummy-row)`).forEach(row => {
    row.style.display = row.getAttribute('data-opponent').includes(q) ? '' : 'none';
  });
  updateGameLogTotals(tableId);
}

const gameLogSortState = {};
function sortGameLog(tableId, colIndex, forceDir) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  const th = table.querySelectorAll('th')[colIndex];
  const isNum = th.getAttribute('data-type') === 'num';

  const asc = forceDir ? forceDir === 'asc' : gameLogSortState[tableId + colIndex] !== 'asc';
  gameLogSortState[tableId + colIndex] = asc ? 'asc' : 'desc';

  const trs = Array.from(tbody.querySelectorAll('tr:not(.game-log-dummy-row)'));
  trs.sort((a, b) => {
    const aCell = a.children[colIndex];
    const bCell = b.children[colIndex];
    let aVal, bVal;
    if (isNum) {
      aVal = aCell.dataset.sort !== undefined ? parseFloat(aCell.dataset.sort) : (parseFloat(aCell.textContent) || 0);
      bVal = bCell.dataset.sort !== undefined ? parseFloat(bCell.dataset.sort) : (parseFloat(bCell.textContent) || 0);
    } else {
      aVal = aCell.textContent.trim().toLowerCase();
      bVal = bCell.textContent.trim().toLowerCase();
    }
    if (aVal < bVal) return asc ? -1 : 1;
    if (aVal > bVal) return asc ? 1 : -1;
    return 0;
  });

  trs.forEach(r => tbody.appendChild(r));
  table.querySelectorAll('th').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
  th.classList.add(asc ? 'sort-asc' : 'sort-desc');
}

// ── Generic per-player history cache + loader ───────────────────────────
// name -> raw rows from /api/player_history. Shared across whichever page
// includes this file, so switching pages doesn't lose the fetch cache
// within a single page's lifetime (each page load starts fresh).
const historyDataCache = {};

async function loadPlayerHistory(name, panelEl, hide2025) {
  if (!historyDataCache[name]) {
    const res = await fetch(`/api/player_history?name=${encodeURIComponent(name)}`);
    historyDataCache[name] = await res.json();
  }
  const { history, projection } = historyDataCache[name];
  panelEl.innerHTML = renderHistoryTable(history, hide2025, panelEl.id, projection);
  initGameLogSort(panelEl.id);
}
