"""Retry contention and concrete JSON-flag behavior, with isolated storage."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from permutation_probe import Store
from test_workspace import BrowserTests

OUT=Path(__file__).resolve().parents[1]/'proof/permutations'


def main():
    rows=[]
    for workers in (1,2,4,8,16):
        for distinct in (1,8,96):
            with tempfile.TemporaryDirectory() as d:
                store=Store(Path(d)/'retry.db');wid=store.create('Retries')
                store.command(wid,'add_agent',{'name':'Mira','personality':'Probe'})
                store.command(wid,'start_simulation',{})
                before=store.read(wid)
                def deliver(i):return store.command(wid,'intervene',{'intervention':'timber','command_id':str(i%distinct)})
                with ThreadPoolExecutor(max_workers=workers) as pool:list(pool.map(deliver,range(96)))
                after=store.read(wid)
                assert after['simulation']['wood']==3+3*distinct
                assert after['entry_count']==before['entry_count']+distinct
                rows.append({'workers':workers,'requests':96,'unique_commands':distinct,'deliveries':(after['simulation']['wood']-3)//3,'passed':True})
    case=BrowserTests();case.setUp();flags=[]
    try:
        wid=case.app.store.create('Flag reproduction')
        def post(body):
            response=case.page.request.post(case.url+'/api/run',headers={'X-Workspace-Client':'1'},data={'id':wid,**body})
            return response.status,response.json()
        code,data=post({'client':'owner','running':'false'})
        flags.append({'case':'string false starts simulation','status':code,'running':data.get('running'),'controller':data.get('controller')})
        code,data=post({'client':'observer','running':True,'takeover':'false'})
        flags.append({'case':'string false takes control','status':code,'running':data.get('running'),'controller':data.get('controller'),'owner_after':case.app.controller})
        post({'client':'observer','running':False})
    finally:case.doCleanups()
    result={'retry_cases':rows,'flag_reproductions':flags}
    (OUT/'concurrency-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
