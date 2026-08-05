// Shared grouped roster picker (Starters / Bench / IR) used anywhere a
// manager needs to choose a player to drop: Player Add/Drop, Waiver claims,
// and the Summer/Winter Transfer Drafts.

function renderRosterPickerGroup(label, rows, badgeFn, onClickFnName) {
  if (!rows.length) return '';
  return `<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);margin:10px 0 4px">${label}</div>` +
    rows.map(r => {
      const badge = badgeFn(r);
      return `
      <button type="button" class="action-btn" style="display:flex;align-items:center;gap:8px;width:100%;text-align:left;margin-bottom:6px" onclick="${onClickFnName}('${r.player_name.replace(/'/g, "\\'")}')">
        <span class="pos-badge pos-${badge}">${badge}</span> ${r.player_name}
      </button>`;
    }).join('');
}

function renderRosterPicker(roster, onClickFnName) {
  onClickFnName = onClickFnName || 'confirmSwap';
  const order = { GK: 0, DEF: 1, MID: 2, FW: 3 };
  const starters = roster.filter(r => r.slot_type === 'starter').sort((a, b) => (order[a.position_slot] ?? 9) - (order[b.position_slot] ?? 9));
  const bench = roster.filter(r => r.slot_type === 'bench');
  const ir = roster.filter(r => r.slot_type === 'ir');
  return renderRosterPickerGroup('Starters', starters, r => r.position_slot || '—', onClickFnName)
    + renderRosterPickerGroup('Bench', bench, () => 'BENCH', onClickFnName)
    + renderRosterPickerGroup('IR', ir, () => 'IR', onClickFnName);
}
