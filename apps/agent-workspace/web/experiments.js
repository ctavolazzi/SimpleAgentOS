// Read-only experiment review; commands remain explicit.
const $ = id => document.getElementById(id);
const node = (tag, text) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
};
const value = item => item === null ? 'Not available' : String(item);

export function experiments({api, task, current, command}) {
  let context = null, latest = null;
  async function load(checkpoint = '') {
    const requested = context;
    $('comparison-status').textContent = 'Reading saved state…';
    const data = await api(`/api/comparison?id=${encodeURIComponent(requested)}${checkpoint ? '&checkpoint=' + encodeURIComponent(checkpoint) : ''}`);
    if (requested !== context || current().wid !== requested) return;
    latest = data.comparison;
    const select = $('comparison-baseline');
    select.replaceChildren();
    for (const item of data.checkpoints) {
      const option = node('option', `${item.label}${item.workspace_id !== requested ? ' · branch source' : ''}`);
      option.value = item.id;
      select.append(option);
    }
    select.disabled = !latest;
    $('comparison-download').disabled = !latest;
    const content = $('comparison-content');
    content.replaceChildren();
    if (!latest) {
      $('comparison-status').textContent = 'No baseline yet. Save a checkpoint, change the environment or advance the simulation, then compare.';
      return;
    }
    select.value = latest.checkpoint.id;
    $('comparison-status').textContent = `Tick ${latest.baseline_tick} → ${latest.current_tick} · ${latest.new_entry_count} new journal records · Snapshot ${new Date(latest.read_at * 1000).toLocaleTimeString()}`;
    const table = node('table');
    table.className = 'comparison-table';
    const caption = node('caption', 'Saved baseline and current workspace');
    table.append(caption);
    const head = node('thead'), header = node('tr');
    for (const label of ['Measure', 'Baseline', 'Current', 'Change']) { const cell = node('th', label); cell.scope = 'col'; header.append(cell); }
    head.append(header); table.append(head);
    const body = node('tbody');
    for (const item of latest.metrics) {
      const row = node('tr'), label = node('th', item.label); label.scope = 'row';
      const delta = item.delta === null ? (item.before === item.after ? 'Unchanged' : 'Changed') : (item.delta > 0 ? '+' : '') + item.delta;
      row.append(label, node('td', value(item.before)), node('td', value(item.after)), node('td', delta));
      body.append(row);
    }
    table.append(body); content.append(table, node('h3', 'Agent activity'));
    for (const agent of latest.agents) content.append(node('p', `${agent.name}${agent.added ? ' · added since baseline' : ''}: ${value(agent.role_before)} → ${agent.role_after}. Completed tasks ${value(agent.tasks_before)} → ${agent.tasks_after}. ${agent.new_entries} new records.`));
    content.append(node('h3', 'New journal records'));
    content.append(node('p', Object.entries(latest.event_counts).map(([kind, count]) => `${count} ${kind}`).join(' · ') || 'No new records since this checkpoint.'));
    content.append(node('p', 'Showing the latest 20 new records. Inspect individual journals for complete history.'));
    for (const entry of latest.recent_entries) {
      const row = node('article'); row.className = 'memory-entry';
      row.append(node('small', `Tick ${entry.tick} · ${entry.actor_name || entry.author_agent_id} · ${entry.kind} · ${entry.source}`), node('p', entry.text));
      const details = node('details'), raw = node('pre', JSON.stringify(entry, null, 2)); raw.className = 'stored-json';
      details.append(node('summary', 'Inspect stored entry JSON'), raw); row.append(details); content.append(row);
    }
  }
  $('compare-button').onclick = () => task(async () => {
    context = current().wid;
    await load();
    $('comparison-dialog').showModal();
  });
  $('comparison-baseline').onchange = () => task(() => load($('comparison-baseline').value));
  $('comparison-refresh').onclick = () => task(() => load($('comparison-baseline').value));
  $('comparison-download').onclick = () => {
    if (!latest) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify({format: 'fieldwork-comparison-v1', ...latest}, null, 2) + '\n'], {type: 'application/json'}));
    const a = node('a'); a.href = url; a.download = `comparison-${latest.checkpoint.id}.json`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  $('named-checkpoint-form').onsubmit = e => {
    e.preventDefault();
    task(async () => {
      await command('checkpoint', {label: $('checkpoint-name').value});
      $('checkpoint-name').value = '';
      $('checkpoints-dialog').close();
      context = current().wid;
      await load();
      $('comparison-dialog').showModal();
    });
  };
}
