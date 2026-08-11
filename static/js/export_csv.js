// Generic "export the currently visible table rows to CSV" helper — used by
// the Main Draft pool table and the Add/Drop & Waivers browse table. Reads
// straight off the rendered DOM so it naturally respects whatever
// search/filter/sort state is currently applied (rows hidden via
// style.display='none' are skipped, and DOM order reflects the active
// sort), rather than re-deriving that state or hitting the server again.
// Header/data cells marked class="no-export" (action buttons, checkboxes)
// are left out of the download.

function csvCell(text) {
  const s = String(text ?? '');
  if (/[",\r\n]/.test(s)) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function cellText(cell) {
  if (!cell) return '';
  const clone = cell.cloneNode(true);
  clone.querySelectorAll('.expand-caret').forEach(el => el.remove());
  // Position-eligibility badges (e.g. FW/MID) render as adjacent <span>s
  // with no separator between them — textContent alone would glue them
  // into "FWMID", so join them explicitly when present.
  const badges = clone.querySelectorAll('.pos-badge');
  if (badges.length) {
    return Array.from(badges).map(b => b.textContent.trim()).join('/');
  }
  return clone.textContent.replace(/\s+/g, ' ').trim();
}

// `extra`, if given, pulls additional columns out of a JSON blob stashed in
// a data attribute on each row (e.g. data-stats='{"goals":3,...}') rather
// than the visible cells — for stat breakdowns that aren't rendered as
// their own <td>s but should still be in the download. Shape:
// {attr: 'data-stats', cols: [[jsonKey, headerLabel], ...]}.
function exportTableToCSV(tableId, rowClass, filename, extra) {
  const table = document.getElementById(tableId);
  if (!table) return;

  const headerCells = Array.from(table.querySelectorAll('thead th'));
  const colIndexes = headerCells
    .map((th, i) => (th.classList.contains('no-export') ? -1 : i))
    .filter(i => i !== -1);

  const headerRow = colIndexes.map(i => csvCell(cellText(headerCells[i])));
  if (extra) headerRow.push(...extra.cols.map(([, label]) => csvCell(label)));
  const lines = [headerRow.join(',')];

  Array.from(table.querySelectorAll(`tbody tr.${rowClass}`))
    .filter(row => row.style.display !== 'none')
    .forEach(row => {
      const values = colIndexes.map(i => csvCell(cellText(row.children[i])));
      if (extra) {
        let data = {};
        try { data = JSON.parse(row.getAttribute(extra.attr) || '{}'); } catch (e) { /* leave blank on bad data */ }
        values.push(...extra.cols.map(([key]) => csvCell(data[key] ?? 0)));
      }
      lines.push(values.join(','));
    });

  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
