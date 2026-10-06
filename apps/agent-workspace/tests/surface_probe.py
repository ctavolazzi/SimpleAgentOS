"""Isolated HTTP validation and browser layout permutations. Records all findings."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_workspace import BrowserTests

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'proof/permutations'
OUT.mkdir(exist_ok=True)


def run():
    case=BrowserTests();case.setUp()
    result={'http':[],'layouts':[],'page_errors':[]}
    try:
        case.app.store.tutorial('skipped')
        wid=case.app.store.create('HTTP probe')
        case.app.store.command(wid,'add_agent',{'name':'Mira','personality':'Probe'})
        state=case.app.store.command(wid,'start_simulation',{})
        agent=state['agents'][0]['id']
        bad=[None,[],{},0,True,'','   ','x'*1201]
        commands=[]
        for value in bad:
            commands.extend([('add_agent',{'name':value,'personality':'Probe'}),('add_agent',{'name':'Probe','personality':value}),('set_role',{'agent_id':agent,'role':value}),('intervene',{'intervention':value}),('decision',{'agent_id':agent,'text':'Probe','evidence_ids':value})])
        for action,body in commands:
            before=case.app.store.read(wid)
            response=case.page.request.post(case.url+'/api/command',headers={'X-Workspace-Client':'1'},data={'id':wid,'action':action,**body})
            after=case.app.store.read(wid)
            result['http'].append({'action':action,'body':body,'status':response.status,'expected':400,'state_unchanged':before==after})
        # JSON booleans are a contract, not generic truthiness. Probe both run flags.
        for field in ('running','takeover'):
            for value in (None,[],{},0,1,'false','true'):
                body={'id':wid,'running':False,field:value}
                response=case.page.request.post(case.url+'/api/run',headers={'X-Workspace-Client':'1'},data=body)
                result['http'].append({'action':'run','body':body,'status':response.status,'expected':400})
                case.app.running=None;case.app.controller=None
        p=case.page
        surfaces=[('workspace',None,None),('journal','memory-button','memory-dialog'),('comparison','compare-button','comparison-dialog'),('checkpoints','checkpoints-button','checkpoints-dialog'),('diagnostics','diagnostics-button','diagnostics-dialog'),('architecture','architecture-button','architecture-dialog'),('guide','guide-button','guide-dialog')]
        for population in (1,3,6,12):
            wid=case.app.store.create('Viewport '+str(population))
            name='<img src=x onerror=alert(1)>' if population==6 else ('W'*40 if population==12 else 'Mira')
            for i in range(population):case.app.store.command(wid,'add_agent',{'name':name if i==0 else 'Agent '+str(i),'personality':'Patient researcher'})
            case.app.store.command(wid,'start_simulation',{})
            case.app.store.command(wid,'checkpoint',{'label':'Baseline'})
            for _ in range(4):case.app.store.command(wid,'step',{})
            p.goto(case.url+'/?probe='+str(population)+'#workspace='+wid)
            p.get_by_role('button',name='Inspect '+name,exact=True).click()
            p.locator('#selected-name').wait_for(state='visible')
            for width in (320,390,700,768,1150,1440):
                p.set_viewport_size({'width':width,'height':900 if width>700 else 844})
                for surface,button,dialog in surfaces:
                    row={'population':population,'width':width,'surface':surface}
                    try:
                        if button:
                            p.locator('#'+button).click(timeout=5000)
                            p.locator('#'+dialog).wait_for(state='visible')
                        overflow=p.evaluate('document.documentElement.scrollWidth > innerWidth')
                        inner=p.locator('#'+dialog).evaluate('(el)=>el.scrollWidth>el.clientWidth') if dialog else False
                        row.update(document_overflow=overflow,dialog_overflow=inner)
                        if overflow or inner:
                            row['wide_elements']=p.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().width && e.getBoundingClientRect().right>innerWidth+1).map(e=>e.id||e.className||e.tagName).slice(0,12)")
                            if dialog:row['dialog_offenders']=p.locator('#'+dialog).evaluate('(root)=>[...root.querySelectorAll("*")].filter(e=>e.getBoundingClientRect().right>root.getBoundingClientRect().right).map(e=>e.id||e.className||e.tagName).slice(0,12)')
                            p.screenshot(path=str(OUT/f'overflow-{population}-{width}-{surface}.png'))
                        if dialog:p.locator('#'+dialog+' [data-close]').click()
                    except Exception as exc:
                        row['error']=str(exc)[:500]
                        p.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
                    result['layouts'].append(row)
                print('Browser population/width:',population,width,flush=True)
        result['page_errors']=case.errors
        result['external_requests']=case.external
    finally:
        (OUT/'surface-summary.json').write_text(json.dumps(result,indent=2)+'\n')
        case.doCleanups()
    print('HTTP cases:',len(result['http']),'unexpected statuses:',sum(r['status']!=r['expected'] for r in result['http']),flush=True)
    print('Layout cases:',len(result['layouts']),'findings:',sum(bool(r.get('error') or r.get('document_overflow') or r.get('dialog_overflow')) for r in result['layouts']),flush=True)

if __name__=='__main__':run()
