"""Read-only checkpoint differences. Deltas describe change, not its cause."""


def compare(baseline, current, entries):
    before = baseline['state']
    old_ids = {entry['id'] for entry in baseline['entries']}
    added = [entry for entry in entries if entry['id'] not in old_ids]
    metrics = []

    def metric(label, old, new):
        numeric = type(old) in (int, float) and type(new) in (int, float)
        metrics.append({'label': label, 'before': old, 'after': new,
                        'delta': round(new - old, 6) if numeric else None})

    metric('Tick', before['tick'], current['tick'])
    metric('Journal entries', len(baseline['entries']), len(entries))
    metric('Agents', len(before['agents']), len(current['agents']))
    for key, label in [('water', 'Water'), ('wood', 'Timber'), ('food', 'Food'),
                       ('moisture', 'Soil moisture (%)'), ('growth', 'Growth (%)'),
                       ('pump', 'Pump'), ('weather', 'Weather'), ('repairs', 'Repairs'),
                       ('harvests', 'Harvests'), ('exchanges', 'Knowledge exchanges')]:
        metric(label, (before.get('simulation') or {}).get(key), (current.get('simulation') or {}).get(key))
    counts = {}
    for entry in added:
        counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
    previous = {agent['id']: agent for agent in before['agents']}
    agents = []
    for agent in current['agents']:
        old = previous.get(agent['id'])
        agents.append({'id': agent['id'], 'name': agent['name'], 'added': old is None,
                       'role_before': old.get('role', old.get('behavior')) if old else None,
                       'role_after': agent.get('role', agent.get('behavior')),
                       'tasks_before': old.get('completed_tasks', 0) if old else None,
                       'tasks_after': agent.get('completed_tasks', 0),
                       'new_entries': sum(e['author_agent_id'] == agent['id'] for e in added)})
    return {'workspace_id': current['id'], 'workspace_name': current['name'],
            'baseline_tick': before['tick'], 'current_tick': current['tick'],
            'metrics': metrics, 'agents': agents, 'new_entry_count': len(added),
            'event_counts': counts, 'recent_entries': list(reversed(added[-20:]))}
