"""Deterministic, saved-state simulation. No model calls or external services.

Knowledge is acquired through observation or explicit exchange. Plans retain the
journal IDs they consulted. Resources and outcomes are committed with those events.
"""
import math

POLICY_VERSION = 1
ROLES = ('scout', 'engineer', 'gardener')


def initialize(state):
    if state.get('simulation'):
        raise ValueError('This world already has a living simulation.')
    state['simulation'] = {'version': 1, 'policy_version': POLICY_VERSION,
        'water': 5, 'wood': 3, 'food': 4, 'moisture': 24, 'growth': 0,
        'pump': 'broken', 'harvests': 0, 'repairs': 0, 'exchanges': 0,
        'weather': 'clear', 'weather_until': 0, 'art_version': 'fieldwork-v2',
        'started_tick': state['tick'], 'has_scout': True}
    for index, agent in enumerate(state['agents']):
        prepare(agent, index)


def prepare(agent, index):
    agent.setdefault('role', ROLES[index % len(ROLES)])
    agent.setdefault('knowledge', {})
    agent.setdefault('energy', 100)
    agent.setdefault('completed_tasks', 0)
    agent.setdefault('plan', None)
    agent.setdefault('working', 0)


def choose(agent, sim):
    """Return goal and evidence keys. No hidden access to another agent's memory."""
    known = agent['knowledge']
    if agent['energy'] < 20:
        return 'rest', []
    if agent['role'] == 'engineer':
        if sim['pump'] == 'broken':
            if 'repair_manual' in known and sim['wood'] >= 2:
                return 'repair', ['repair_manual']
            return ('inspect_pump' if sim.get('has_scout') else 'study'), []
        return 'maintain', ['repair_manual'] if 'repair_manual' in known else []
    if agent['role'] == 'gardener':
        if sim['growth'] >= 100:
            return 'harvest', ['garden_report'] if 'garden_report' in known else []
        if 'garden_report' not in known:
            return 'inspect_garden', []
        if sim['moisture'] < 65 and sim['water'] > 0:
            return 'water', ['garden_report']
        return 'tend', ['garden_report']
    if 'repair_manual' not in known:
        return 'study', []
    if 'garden_report' not in known:
        return 'inspect_garden', []
    return ('inspect_pump', ['repair_manual']) if sim['pump'] == 'broken' else ('inspect_garden', ['garden_report'])


DESTINATIONS = {'rest': 'library', 'study': 'library', 'repair': 'workshop',
    'maintain': 'workshop', 'inspect_pump': 'workshop', 'inspect_garden': 'garden',
    'water': 'garden', 'tend': 'garden', 'harvest': 'garden'}
DURATIONS = {'rest': 20, 'study': 8, 'repair': 16, 'maintain': 10,
    'inspect_pump': 5, 'inspect_garden': 5, 'water': 8, 'tend': 10, 'harvest': 12}


def tick(state, emit):
    sim = state['simulation']
    if sim['version'] != 1 or sim['policy_version'] != POLICY_VERSION:
        raise ValueError('Unsupported simulation policy version.')
    sim['has_scout'] = any(a.get('role') == 'scout' and not a['archived'] for a in state['agents'])
    state['tick'] += 1
    now = state['tick']
    if sim['weather'] == 'rain' and now >= sim['weather_until']:
        sim['weather'] = 'clear'
    if now % 8 == 0:
        sim['moisture'] = max(0, min(100, sim['moisture'] + (5 if sim['weather'] == 'rain' else -2)))
        if sim['pump'] == 'working': sim['water'] = min(30, sim['water'] + 1)
        if sim['moisture'] >= 35: sim['growth'] = min(100, sim['growth'] + 4)
    places = {p['id']: p for p in state['places']}
    for index, a in enumerate(state['agents']):
        if a['archived']: continue
        prepare(a, index)
        if not a['plan']:
            goal, keys = choose(a, sim)
            evidence = [a['knowledge'][key]['entry_id'] for key in keys]
            record = emit(a, 'plan', f"{a['role'].title()} chose {goal.replace('_', ' ')}. " + ('Uses remembered evidence.' if evidence else 'A fresh observation or rest is needed.'), evidence)
            a['plan'] = {'goal': goal, 'entry_id': record['id'], 'evidence_ids': evidence}
            a['target'] = next(i for i,p in enumerate(state['places']) if p['id'] == DESTINATIONS[goal])
        goal = a['plan']['goal']
        dest = places[DESTINATIONS[goal]]
        dx, dy = dest['x']-a['x'], dest['y']-a['y']
        if dx or dy:
            # Same orthogonal path used by the original scene.
            if dx:
                axis, end = ('y', 126) if a['y'] != 126 else ('x', dest['x'])
            else:
                axis, end = 'y', dest['y']
            delta = end-a[axis]
            a['facing'] = ('east' if delta>0 else 'west') if axis=='x' else ('south' if delta>0 else 'north')
            a[axis] += max(-4, min(4, delta))
            a['activity'] = f"Going to {dest['name'].lower()}: {goal.replace('_', ' ')}"
            continue
        a['working'] += 1
        a['activity'] = f"{goal.replace('_', ' ').title()} ({a['working']}/{DURATIONS[goal]})"
        if a['working'] < DURATIONS[goal]: continue
        cause = [a['plan']['entry_id']]
        text = None
        knowledge = None
        if goal == 'study':
            text = 'Read the pump repair guide: replace the seal using two units of timber.'
            knowledge = 'repair_manual'
        elif goal == 'repair':
            if sim['pump'] == 'broken' and sim['wood'] >= 2 and 'repair_manual' in a['knowledge']:
                sim['wood'] -= 2; sim['pump'] = 'working'; sim['repairs'] += 1
                text = 'Repaired the pump using the shared guide and two timber. Water can replenish.'
            else: text = 'Repair conditions changed; no materials spent. Replanning.'
        elif goal == 'water':
            if sim['water'] > 0:
                used = min(3, sim['water']); sim['water'] -= used
                sim['moisture'] = min(100, sim['moisture'] + used*12)
                text = f'Watered the beds with {used} water. Soil moisture is now {sim["moisture"]}%.'
            else: text = 'Reservoir empty; no watering performed.'
        elif goal == 'harvest':
            if sim['growth'] >= 100:
                sim['food'] += 4; sim['growth'] = 0; sim['harvests'] += 1
                text = 'Harvested four food. A new growing cycle begins.'
            else: text = 'Crop not ready; waiting for growth.'
        elif goal == 'rest':
            fed = sim['food'] > 0
            if fed: sim['food'] -= 1
            a['energy'] = 100 if fed else 60
            text = 'Ate one food and rested at the library; energy restored.' if fed else 'Rested without a meal; energy recovered to 60%.'
        elif goal in ('inspect_garden', 'tend'):
            text = f'Observed the garden: moisture {sim["moisture"]}%, growth {sim["growth"]}%.'
            knowledge = 'garden_report'
        elif goal == 'inspect_pump':
            text = f'Observed the pump: {sim["pump"]}; reservoir {sim["water"]} water.'
            knowledge = 'pump_report'
        else:
            text = 'Checked the workshop and working pump.'
        observation = goal in ('study','inspect_garden','tend','inspect_pump')
        record = emit(a, 'observation' if observation else 'outcome', text, cause)
        if knowledge:
            a['knowledge'][knowledge] = {'entry_id': record['id'], 'tick': now, 'from': a['id']}
            # Explicit field-radio exchange. Fresh observations are exchanged once.
            for b in state['agents']:
                if b['id'] == a['id'] or b['archived']: continue
                prepare(b, state['agents'].index(b))
                prior = b['knowledge'].get(knowledge)
                if prior and knowledge == 'repair_manual': continue
                received = emit(b, 'exchange', f'{a["name"]} shared: {text}', [record['id']])
                b['knowledge'][knowledge] = {'entry_id': received['id'], 'tick': now, 'from': a['id']}
                sim['exchanges'] += 1
                if b['role'] == 'engineer' and knowledge == 'repair_manual' and b.get('plan',{}):
                    if b['plan']['goal'] in ('study', 'inspect_pump'):
                        emit(b, 'replan', 'Shared repair knowledge changed my next action.', [received['id'], b['plan']['entry_id']])
                        b['plan'] = None; b['working'] = 0
        a['completed_tasks'] += 1
        a['energy'] = max(0, a['energy'] - (0 if goal=='rest' else 8))
        a['visits'] += 1
        a['plan'] = None; a['working'] = 0; a['target'] = None
