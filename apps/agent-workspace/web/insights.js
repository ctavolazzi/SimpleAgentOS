// Read-only memory inspection plus explicitly human-authored decisions.
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
};

export function insights({api, task, current, refresh}) {
  let entries = [], cursor = null, generation = 0;
  const chosen = new Set();
  let context = null;

  function storedData(entry) {
    const details = node('details');
    details.append(node('summary', 'Inspect stored entry JSON'), node('pre', JSON.stringify(entry, null, 2), 'stored-json'));
    return details;
  }

  async function snapshot() {
    const requested = context;
    const data = await api(`/api/journal/export?id=${encodeURIComponent(requested.wid)}&agent=${encodeURIComponent(requested.agent.id)}`);
    if (requested !== context) throw Error('Selected journal changed. Open it again.');
    $('journal-json').textContent = JSON.stringify(data, null, 2);
    $('journal-data-status').textContent = `${data.entry_count} entries, oldest first. Snapshot at ${new Date(data.exported_at * 1000).toLocaleString()}. Includes all records regardless of search. Reopen to refresh.`;
    return data;
  }

  $('journal-data').ontoggle = () => {
    if ($('journal-data').open) task(snapshot);
  };
  $('journal-export').onclick = () => task(async () => {
    const data = await snapshot();
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + '\n'], {type: 'application/json'}));
    const link = node('a');
    link.href = url;
    link.download = `journal-${data.journal_id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  function draw() {
    const list = $('memory-entries');
    list.replaceChildren();
    const graph = $('memory-map');
    graph.replaceChildren();
    for (const entry of entries) {
      const row = node('article', undefined, 'memory-entry');
      row.id = 'memory-' + entry.id;
      row.append(node('small', `Tick ${entry.tick} · ${entry.kind} · ${entry.source} · ${entry.actor_name || context.agent.name}`), node('p', entry.text));
      row.append(node('small', `Recorded ${new Date(entry.created * 1000).toLocaleString()} · Entry ${entry.id}`), storedData(entry));
      if (entry.kind === 'observation') {
        const label = node('label', undefined, 'evidence-choice');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = chosen.has(entry.id);
        box.onchange = () => {
          if (box.checked) chosen.add(entry.id); else chosen.delete(entry.id);
          $('evidence-count').textContent = `${chosen.size} observations selected`;
        };
        label.append(box, node('span', 'Use as evidence'));
        row.append(label);
      }
      for (const id of entry.evidence_ids || []) {
        const link = node('button', 'Trace record ' + id.slice(0, 8), 'text-button');
        link.onclick = () => task(() => trace(id));
        row.append(link);
      }
      list.append(row);
      const chip = node('button', `T${entry.tick} · ${entry.kind}`, 'memory-node');
      chip.dataset.kind = entry.kind;
      chip.title = entry.text;
      chip.onclick = () => row.scrollIntoView({block: 'center'});
      graph.append(chip);
      if (entry.evidence_ids?.length) graph.append(node('small', `← ${entry.evidence_ids.length} linked observations`));
    }
    if (!entries.length) list.append(node('p', 'No matching records. This is the current saved journal.', 'help'));
    $('memory-more').hidden = cursor === null;
  }

  async function trace(id) {
    const entry = await api(`/api/entry?id=${encodeURIComponent(context.wid)}&entry=${encodeURIComponent(id)}`);
    const panel = $('memory-trace');
    panel.replaceChildren(node('h3', 'Source record'), node('small', `Tick ${entry.tick} · ${entry.kind} · ${entry.actor_name || entry.author_agent_id} · ${entry.source}`), node('p', entry.text));
    for (const source of entry.evidence_ids || []) {
      const button = node('button', 'Trace earlier cause ' + source.slice(0, 8));
      button.onclick = () => task(() => trace(source));
      panel.append(button);
    }
    panel.append(storedData(entry));
    panel.hidden = false;
    panel.scrollIntoView({block:'nearest'});
  }

  async function load(reset = false) {
    const token = ++generation;
    const requested = context;
    const query = $('memory-search').value;
    const before = !reset && cursor !== null ? `&before=${cursor}` : '';
    const data = await api(`/api/journal?id=${encodeURIComponent(requested.wid)}&agent=${encodeURIComponent(requested.agent.id)}&q=${encodeURIComponent(query)}${before}`);
    if (token !== generation || requested !== context) return;
    entries = reset ? data.items : entries.concat(data.items);
    cursor = data.next;
    $('memory-status').textContent = `${entries.length} of ${data.total} matching records · Read ${new Date().toLocaleTimeString()}`;
    draw();
  }

  $('memory-button').onclick = () => task(async () => {
    const {state, wid, selected} = current();
    const agent = state.agents.find(a => a.id === selected);
    if (!agent) throw Error('Select an agent first.');
    context = {wid, agent};
    entries = []; cursor = null; chosen.clear();$('memory-trace').hidden=true;
    $('memory-search').value = '';
    $('decision-text').value = '';
    $('evidence-count').textContent = '0 observations selected';
    $('memory-title').textContent = `${agent.name}'s journal`;
    $('journal-identity').textContent = `Journal ${agent.journal_id} · Agent ${agent.id} · Workspace ${wid}`;
    $('journal-data').open = false;
    $('journal-json').textContent = '';
    $('journal-data-status').textContent = '';
    await load(true);
    $('memory-dialog').showModal();
  });
  $('memory-search-form').onsubmit = e => { e.preventDefault(); task(() => load(true)); };
  $('memory-more').onclick = () => task(() => load());
  $('decision-form').onsubmit = e => {
    e.preventDefault();
    task(async () => {
      await api('/api/command', {id: context.wid, action: 'decision', agent_id: context.agent.id,
        text: $('decision-text').value, evidence_ids: [...chosen]});
      chosen.clear();
      $('decision-text').value = '';
      $('evidence-count').textContent = 'Decision saved with observation links. No truth or relevance judgment is implied.';
      await load(true);
      await refresh();
    });
  };
  $('diagnostics-button').onclick = () => task(async () => {
    const data = await api('/api/diagnostics?id=' + encodeURIComponent(current().wid));
    $('diagnostics-status').textContent = `${data.forwarding ? 'Wiretap forwarding enabled' : 'Stored locally; Wiretap forwarding off'} · ${data.pending} unacknowledged events · Read ${new Date().toLocaleTimeString()}`;
    const list = $('diagnostics-list'); list.replaceChildren();
    for (const event of data.events) {
      const p = event.payload;
      list.append(node('p', `${new Date(p.extra.created * 1000).toLocaleTimeString()} · ${p.level} · ${p.msg} · ${event.delivered ? 'forwarded' : 'local'}${event.error ? ' · ' + event.error : ''}`));
    }
    if (!data.events.length) list.append(node('p', 'No diagnostic events yet.'));
    $('diagnostics-dialog').showModal();
  });
  $('architecture-button').onclick = () => task(async () => {
    const data = await api('/api/architecture');
    const list = $('architecture-list'); list.replaceChildren();
    for (const item of data.nodes) {
      const row = node('article', undefined, 'memory-entry');
      row.append(node('h3', item.name), node('p', item.description), node('code', item.path));
      row.append(node('small', `Source SHA-256: ${item.sha256}`));
      list.append(row);
    }
    for (const edge of data.edges) list.append(node('p', `${edge.from} → ${edge.to}: ${edge.label}`));
    $('architecture-dialog').showModal();
  });
}
