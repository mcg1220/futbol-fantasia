// Shared in-season player trade modal — used from the Team page (propose a
// trade with the manager whose team you're viewing) and the Add/Drop &
// Waivers page ("Propose Trade" next to a player owned by someone else).
// Requires each including page to declare `CURRENT_MANAGER_ID` and to
// include a `<div id="trade-modal">...</div>` matching the markup below
// (same per-page-modal convention as team.html's #swap-modal).

let tradeTargetManagerId = null;

function renderTradeCheckboxGroup(label, rows, badgeFn, side) {
  if (!rows.length) return '';
  return `<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);margin:10px 0 4px">${label}</div>` +
    rows.map(r => {
      const badge = badgeFn(r);
      const id = `trade-${side}-${r.player_name.replace(/[^a-zA-Z0-9]/g, '_')}`;
      return `
      <label for="${id}" style="display:flex;align-items:center;gap:8px;width:100%;padding:4px 2px;cursor:pointer">
        <input type="checkbox" id="${id}" value="${r.player_name.replace(/"/g, '&quot;')}" data-side="${side}" />
        <span class="pos-badge pos-${badge}">${badge}</span> ${r.player_name}
      </label>`;
    }).join('');
}

function renderTradeRosterList(roster, side) {
  const order = { GK: 0, DEF: 1, MID: 2, FW: 3 };
  const starters = roster.filter(r => r.slot_type === 'starter').sort((a, b) => (order[a.position_slot] ?? 9) - (order[b.position_slot] ?? 9));
  const bench = roster.filter(r => r.slot_type === 'bench');
  const ir = roster.filter(r => r.slot_type === 'ir');
  return renderTradeCheckboxGroup('Starters', starters, r => r.position_slot || '—', side)
    + renderTradeCheckboxGroup('Bench', bench, () => 'BENCH', side)
    + renderTradeCheckboxGroup('IR', ir, () => 'IR', side);
}

async function openTradeModal(targetManagerId, preSelectPlayerName) {
  if (!CURRENT_MANAGER_ID) { alert('Log in first.'); return; }
  tradeTargetManagerId = targetManagerId;
  document.getElementById('trade-modal').style.display = 'flex';
  document.getElementById('trade-give-list').innerHTML = 'Loading...';
  document.getElementById('trade-receive-list').innerHTML = 'Loading...';
  document.getElementById('trade-propose-error').style.display = 'none';

  try {
    const [mineRes, theirsRes] = await Promise.all([
      fetch(`/api/manager/${CURRENT_MANAGER_ID}/roster`),
      fetch(`/api/manager/${targetManagerId}/roster`),
    ]);
    const mine = await mineRes.json();
    const theirs = await theirsRes.json();
    document.getElementById('trade-give-title').textContent = 'Your players to give';
    document.getElementById('trade-receive-title').textContent = 'Their players to receive';
    document.getElementById('trade-give-list').innerHTML = renderTradeRosterList(mine, 'give') || '<div class="empty-state">No players on your roster.</div>';
    document.getElementById('trade-receive-list').innerHTML = renderTradeRosterList(theirs, 'receive') || '<div class="empty-state">No players on their roster.</div>';

    if (preSelectPlayerName) {
      const cb = document.getElementById(`trade-receive-${preSelectPlayerName.replace(/[^a-zA-Z0-9]/g, '_')}`);
      if (cb) cb.checked = true;
    }
  } catch (e) {
    document.getElementById('trade-give-list').innerHTML = '<div class="empty-state">Could not load rosters — try closing and reopening this dialog.</div>';
    document.getElementById('trade-receive-list').innerHTML = '';
    console.error(e);
  }
}

function closeTradeModal() {
  tradeTargetManagerId = null;
  document.getElementById('trade-modal').style.display = 'none';
}

async function proposeTrade() {
  const givePlayers = Array.from(document.querySelectorAll('#trade-give-list input[type=checkbox]:checked')).map(cb => cb.value);
  const receivePlayers = Array.from(document.querySelectorAll('#trade-receive-list input[type=checkbox]:checked')).map(cb => cb.value);
  const errorEl = document.getElementById('trade-propose-error');
  errorEl.style.display = 'none';

  if (!givePlayers.length && !receivePlayers.length) {
    errorEl.textContent = 'Select at least one player to trade.';
    errorEl.style.display = 'block';
    return;
  }

  try {
    const res = await fetch('/api/trade/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_manager_id: tradeTargetManagerId, give_players: givePlayers, receive_players: receivePlayers })
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || 'Could not propose trade.';
      errorEl.style.display = 'block';
      return;
    }
    closeTradeModal();
    window.location.reload();
  } catch (e) {
    errorEl.textContent = 'Network error — please try again.';
    errorEl.style.display = 'block';
    console.error(e);
  }
}

async function respondTrade(tradeId, action) {
  if (action === 'decline' && !confirm('Decline this trade?')) return;
  if (action === 'cancel' && !confirm('Cancel this trade?')) return;
  try {
    const res = await fetch(`/api/trade/${tradeId}/${action}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { alert(data.error || `Could not ${action} trade.`); return; }
    window.location.reload();
  } catch (e) {
    alert('Network error — please try again.');
    console.error(e);
  }
}
