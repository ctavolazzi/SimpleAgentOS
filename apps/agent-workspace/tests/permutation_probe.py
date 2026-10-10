"""Reproducible exploration, not a claim that every configuration should harvest.

Run from any directory: python3 tests/permutation_probe.py
Policy grid runs in memory. Storage/browser cases use temporary databases only.
"""
import collections
import copy
import hashlib
import itertools
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import simulation
from store import Store, PLACES

OUT = ROOT / 'proof/permutations'
OUT.mkdir(exist_ok=True)


def fixture(roles):
    state = {'id': 'probe', 'tick': 0, 'places': copy.deepcopy(PLACES), 'agents': [
        {'id': str(i), 'name': f'Agent {i}', 'role': role, 'archived': False,
         'x': 148 + i * 3, 'y': 126, 'visits': 0, 'target': None}
        for i, role in enumerate(roles)]}
    simulation.initialize(state)
    return state


def invariant(state):
    s = state['simulation']
    for key in ('water', 'wood', 'food', 'moisture', 'growth', 'repairs', 'harvests', 'exchanges'):
        assert s[key] >= 0, ('negative', key, s[key])
    assert s['water'] <= 30 and s['moisture'] <= 100 and s['growth'] <= 100
    for a in state['agents']:
        assert 0 <= a['energy'] <= 100
        assert 0 <= a['x'] <= 320 and 0 <= a['y'] <= 200


def grid():
    broken = fixture(simulation.ROLES); broken['simulation']['water'] = -1
    try: invariant(broken)
    except AssertionError: print('Negative control: negative resource detected', flush=True)
    else: raise AssertionError('Invariant probe cannot detect negative resources')
    rows, failures = [], []
    start = time.perf_counter()
    combinations = itertools.product(itertools.product(simulation.ROLES, repeat=3), (0, 5, 30), (0, 1, 2, 3), (0, 4), (0, 35, 100), ('none', 'rain', 'drought', 'break_pump', 'timber'))
    with (OUT / 'policy-cases.jsonl').open('w') as output:
        for index, (roles, water, wood, food, moisture, intervention) in enumerate(combinations):
            state = fixture(roles)
            state['simulation'].update(water=water, wood=wood, food=food, moisture=moisture)
            ids, kinds = set(), collections.Counter()
            def emit(a, kind, text, evidence):
                assert all(e in ids for e in evidence), ('dangling evidence', evidence)
                entry = {'id': str(len(ids))}
                ids.add(entry['id']); kinds[kind] += 1
                return entry
            row = dict(case=index, roles=roles, water=water, wood=wood, food=food, moisture=moisture, intervention=intervention)
            try:
                for tick in range(480):
                    if tick == 120:
                        s = state['simulation']
                        if intervention == 'rain': s.update(weather='rain', weather_until=240)
                        elif intervention == 'drought': s.update(weather='clear', weather_until=0, moisture=5)
                        elif intervention == 'break_pump': s['pump'] = 'broken'
                        elif intervention == 'timber': s['wood'] += 3
                    simulation.tick(state, emit)
                    invariant(state)
                row.update(harvests=state['simulation']['harvests'], repairs=state['simulation']['repairs'], food_end=state['simulation']['food'], events=len(ids), outcomes=kinds['outcome'], exchanges=kinds['exchange'], tasks=sum(a['completed_tasks'] for a in state['agents']))
            except Exception as exc:
                row['error'] = repr(exc); failures.append(row)
            rows.append(row); output.write(json.dumps(row)+'\n')
            if (index+1)%1000 == 0: print('Policy cases:', index+1, flush=True)
    good = [r for r in rows if 'error' not in r]
    balanced = [r for r in good if len(set(r['roles'])) == 3]
    groups = collections.defaultdict(list)
    for r in balanced:
        groups[tuple(r[k] for k in ('water','wood','food','moisture','intervention'))].append(r)
    order_sensitive = [g for g in groups.values() if len({r['harvests'] for r in g})>1]
    return {'cases': len(rows), 'ticks': len(good)*480, 'seconds': round(time.perf_counter()-start,2), 'failures': failures,
            'no_harvest': sum(r['harvests']==0 for r in good), 'balanced_cases':len(balanced), 'balanced_no_harvest':sum(r['harvests']==0 for r in balanced),
            'balanced_order_groups':len(groups), 'order_sensitive_harvest_groups':len(order_sensitive), 'order_example':order_sensitive[0] if order_sensitive else [],
            'balanced_stall_examples':[r for r in balanced if r['harvests']==0][:6],
            'max_events':max(good,key=lambda r:r['events']),
            'by_intervention':{v:{'cases':sum(r['intervention']==v for r in balanced),'no_harvest':sum(r['intervention']==v and r['harvests']==0 for r in balanced),'harvests':sum(r['harvests'] for r in balanced if r['intervention']==v)} for v in ('none','rain','drought','break_pump','timber')}}


def storage():
    results=[]
    for seed in range(36):
        rng=random.Random(seed)
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'isolated.db'); wid=store.create('Permutation '+str(seed))
            count=(1,3,6,12)[seed%4]
            for i in range(count): store.command(wid,'add_agent',{'name':f'A{i}','personality':'Probe'})
            store.command(wid,'start_simulation',{})
            commands=0
            for step in range(50):
                store.command(wid,'step',{}); commands+=1
                if step%10 == 0:
                    body={'intervention':rng.choice(['rain','drought','break_pump','timber']),'command_id':f'{seed}-{step}'}
                    store.command(wid,'intervene',body); once=store.read(wid)
                    store.command(wid,'intervene',body)
                    assert store.read(wid)==once, 'Replay changed state'
                    commands+=2
            saved=store.command(wid,'checkpoint',{'label':'Branch baseline'}); cp=saved['checkpoints'][0]['id']
            branch=store.restore(cp)
            for _ in range(50):
                store.command(wid,'step',{});store.command(branch,'step',{});commands+=2
            left,right=store.read(wid),Store(Path(d)/'isolated.db').read(branch)
            assert left['simulation']==right['simulation'], 'Restored resources diverged'
            assert left['entry_count']==right['entry_count'], 'Restored event count diverged'
            assert [(a['energy'],a['completed_tasks'],a['x'],a['y']) for a in left['agents']]==[(a['energy'],a['completed_tasks'],a['x'],a['y']) for a in right['agents']]
            for a in right['agents']:
                data=store.export_journal(branch,a['id']); page=store.journal(branch,a['id']); entries=page['items']
                while page['next'] is not None:
                    page=store.journal(branch,a['id'],page['next']);entries+=page['items']
                assert len(entries)==data['entry_count']==len(data['entries'])
                assert {e['id'] for e in entries}=={e['id'] for e in data['entries']}
            comparison=store.compare_checkpoint(branch,cp)['comparison']
            assert comparison['new_entry_count']==right['entry_count']-saved['entry_count']
            results.append({'seed':seed,'agents':count,'commands':commands,'entries':right['entry_count']})
        if (seed+1)%6==0: print('Storage sequences:',seed+1,flush=True)
    return {'cases':len(results),'failures':[],'results':results}


def main():
    start=time.perf_counter()
    result={'python':platform.python_version(),'platform':platform.platform(),'source_hashes':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ('simulation.py','store.py','comparison.py','web/app.js','web/insights.js','web/experiments.js')}}
    result['policy']=grid()
    (OUT/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    result['storage']=storage()
    result['elapsed_seconds']=round(time.perf_counter()-start,2)
    (OUT/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result['policy'].items() if k not in ('order_example','balanced_stall_examples','max_events')},indent=2),flush=True)
    print('Results:',OUT,flush=True)
    if result['policy']['failures']:raise SystemExit(1)

if __name__=='__main__':main()
