"""Disentangle initial position from update order, and extend resource-starved runs."""
import copy
import itertools
import json
from pathlib import Path
from permutation_probe import fixture, invariant, simulation

OUT=Path(__file__).resolve().parents[1]/'proof/permutations'


def run(state, ticks, intervention='none'):
    ids=set(); first=None
    def emit(a,kind,text,evidence):
        assert all(e in ids for e in evidence)
        record={'id':str(len(ids))};ids.add(record['id']);return record
    for t in range(ticks):
        if t==120:
            if intervention=='drought':state['simulation'].update(moisture=5,weather='clear',weather_until=0)
            elif intervention=='rain':state['simulation'].update(weather='rain',weather_until=240)
            elif intervention=='timber':state['simulation']['wood']+=3
        simulation.tick(state,emit);invariant(state)
        if first is None and state['simulation']['harvests']:first=state['tick']
    return {'harvests':state['simulation']['harvests'],'repairs':state['simulation']['repairs'],'first_harvest_tick':first,'events':len(ids),'tasks':sum(a['completed_tasks'] for a in state['agents']),'final_resources':state['simulation']}


def main():
    result={'order_controls':[],'long_runs':[]}
    for positions in ('fixed_by_role','colocated'):
        base=fixture(simulation.ROLES)
        base['simulation'].update(water=5,wood=0,food=0,moisture=100)
        if positions=='colocated':
            for a in base['agents']:a['x']=148
        for order in itertools.permutations(range(3)):
            state=copy.deepcopy(base);state['agents']=[state['agents'][i] for i in order]
            result['order_controls'].append({'positions':positions,'order':order,**run(state,480,'drought')})
    for intervention in ('none','rain','timber'):
        state=fixture(simulation.ROLES);state['simulation'].update(water=0,wood=0,food=0,moisture=0)
        result['long_runs'].append({'intervention':intervention,'ticks':5000,**run(state,5000,intervention)})
    (OUT/'followup-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    for positions in ('fixed_by_role','colocated'):
        print(positions,sorted({r['harvests'] for r in result['order_controls'] if r['positions']==positions}))
    for row in result['long_runs']: print(row)

if __name__=='__main__':main()
