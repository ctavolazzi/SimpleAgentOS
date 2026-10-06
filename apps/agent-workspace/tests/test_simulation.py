"""Causal simulation, branch recovery, control ownership and art contracts."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import simulation
from store import Store
import test_workspace as baseline


class SimulationTests(unittest.TestCase):
    setUp = baseline.PersistenceTests.setUp

    def start(self):
        for name in ('Mira', 'Sol', 'Fern'):
            self.store.command(self.wid, 'add_agent', {'name': name, 'personality': 'Field researcher'})
        return self.store.command(self.wid, 'start_simulation', {})

    def test_full_loop_repairs_and_harvests(self):
        self.start()
        for _ in range(400): self.store.command(self.wid, 'step', {})
        state = self.store.read(self.wid)
        self.assertEqual(state['simulation']['repairs'], 1)
        self.assertGreaterEqual(state['simulation']['harvests'], 1)
        self.assertGreater(state['simulation']['exchanges'], 0)
        engineer = state['agents'][1]
        records = self.store.journal(self.wid, engineer['id'])
        items = records['items']
        while records['next']:
            records = self.store.journal(self.wid, engineer['id'], records['next'])
            items += records['items']
        self.assertTrue(any(e['kind'] == 'exchange' and 'repair guide' in e['text'] for e in items))
        self.assertTrue(any(e['kind'] == 'plan' and 'chose repair' in e['text'] and e['evidence_ids'] for e in items))
        self.assertTrue(any(e['kind'] == 'outcome' and 'Repaired' in e['text'] for e in items))

    def test_evidence_changes_engineer_choice(self):
        state = self.start(); engineer = state['agents'][1]
        self.assertEqual(simulation.choose(engineer, state['simulation'])[0], 'inspect_pump')
        engineer['knowledge']['repair_manual'] = {'entry_id': 'observed-guide'}
        self.assertEqual(simulation.choose(engineer, state['simulation']), ('repair', ['repair_manual']))
        engineer['knowledge'].clear()
        self.assertEqual(simulation.choose(engineer, state['simulation'])[0], 'inspect_pump')

    def test_exchange_redirects_engineer(self):
        state = self.start()
        scout, engineer, _ = state['agents']
        scout.update(x=162,y=70,working=7,plan={'goal':'study','entry_id':'study-plan','evidence_ids':[]})
        engineer.update(x=150,y=126,plan={'goal':'study','entry_id':'engineer-study','evidence_ids':[]})
        events = []
        def emit(agent,kind,text,ids):
            item={'id':str(len(events)), 'kind':kind,'text':text,'agent':agent['id'],'evidence':ids};events.append(item);return item
        simulation.tick(state,emit)
        self.assertEqual(engineer['plan']['goal'], 'repair')
        self.assertEqual(engineer['knowledge']['repair_manual']['from'],scout['id'])
        self.assertTrue(engineer['plan']['evidence_ids'])

    def test_restore_continues_equivalent_resources(self):
        self.start()
        for _ in range(90): self.store.command(self.wid,'step',{})
        state=self.store.command(self.wid,'checkpoint',{})
        branch=self.store.restore(state['checkpoints'][0]['id'])
        for _ in range(80):
            self.store.command(self.wid,'step',{})
            self.store.command(branch,'step',{})
        a=self.store.read(self.wid);b=Store(self.path).read(branch)
        self.assertEqual(a['simulation'],b['simulation'])
        self.assertEqual([(x['x'],x['y'],x['completed_tasks'],x['energy']) for x in a['agents']],[(x['x'],x['y'],x['completed_tasks'],x['energy']) for x in b['agents']])
        self.assertEqual(a['entry_count'],b['entry_count'])

    def test_intervention_changes_branch_and_retry_is_idempotent(self):
        self.start()
        saved=self.store.command(self.wid,'checkpoint',{})
        branch=self.store.restore(saved['checkpoints'][0]['id'])
        body={'intervention':'timber','command_id':'delivery-1'}
        self.store.command(branch,'intervene',body)
        self.store.command(branch,'intervene',body)
        self.assertEqual(self.store.read(branch)['simulation']['wood'],6)
        self.assertEqual(self.store.read(self.wid)['simulation']['wood'],3)
        with self.assertRaises(ValueError):self.store.command(branch,'intervene',body|{'intervention':'rain'})
        self.store.command(branch,'intervene',{'intervention':'rain'})
        for _ in range(24):
            self.store.command(branch,'step',{});self.store.command(self.wid,'step',{})
        self.assertGreater(self.store.read(branch)['simulation']['moisture'],self.store.read(self.wid)['simulation']['moisture'])

    def test_failed_outcome_rolls_back_world_and_journal(self):
        self.start()
        before=self.store.read(self.wid)
        with patch.object(self.store,'_entry',side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):self.store.command(self.wid,'step',{})
        after=self.store.read(self.wid)
        self.assertEqual(after['tick'],before['tick'])
        self.assertEqual(after['simulation'],before['simulation'])
        self.assertEqual(after['entry_count'],before['entry_count'])

    def test_art_manifest_matches_real_assets(self):
        manifest=json.loads((ROOT/'art/manifest.json').read_text())
        self.assertGreaterEqual(len(manifest['assets']),11)
        for asset in manifest['assets']:
            data=(ROOT/'web/assets'/asset['path']).read_bytes()
            self.assertTrue(data.startswith(b'\x89PNG'))
            self.assertEqual(hashlib.sha256(data).hexdigest(),asset['sha256'])
        self.assertTrue((ROOT/'art/fieldwork-props.aseprite').stat().st_size>100)
        self.assertTrue((ROOT/'art/pump-flow.aseprite').stat().st_size>100)


class SimulationBrowserTests(unittest.TestCase):
    setUp=baseline.BrowserTests.setUp
    tearDown=baseline.BrowserTests.tearDown
    create_workspace=baseline.BrowserTests.create_workspace

    def test_live_simulation_and_two_tab_control(self):
        from playwright.sync_api import expect
        p=self.page;self.create_workspace();p.get_by_role('button',name='Skip for now').click()
        wid=self.app.store.overview()['workspaces'][0]['id']
        for name in ('Mira','Sol','Fern'):
            self.app.store.command(wid,'add_agent',{'name':name,'personality':'Field researcher'})
        p.get_by_role('button',name='Start living simulation').click()
        expect(p.locator('#metric-pump')).to_have_text('broken')
        p.get_by_role('button',name='Resume',exact=True).click()
        expect(p.locator('#run-state')).to_have_text('Running')
        observer=self.browser.new_page()
        observer.goto(self.url+'/#workspace='+wid)
        expect(observer.locator('#take-control')).to_be_visible()
        expect(observer.locator('#run-state')).to_have_text('Running')
        observer.get_by_role('button',name='Take control',exact=True).click()
        expect(p.locator('#take-control')).to_be_visible()
        observer.get_by_role('button',name='Pause',exact=True).click()
        observer.close()
        expect(p.locator('#run-state')).to_have_text('Paused')
        for _ in range(400):self.app.store.command(wid,'step',{})
        expect(p.locator('#metric-pump')).to_have_text('working')
        expect(p.locator('#sim-outcomes')).to_contain_text('harvests')
        p.get_by_role('button',name='Inspect Fern',exact=True).click()
        expect(p.locator('#agent-sim-details')).to_contain_text('tasks completed')
        p.locator('#intervention').select_option('rain')
        p.get_by_role('button',name='Apply intervention',exact=True).click()
        expect(p.locator('#sim-day')).to_contain_text('rain')
        p.screenshot(path=str(ROOT/'proof/simulation-desktop.png'),full_page=True)
        p.set_viewport_size({'width':390,'height':844})
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'),p.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(0,15)"))
        p.screenshot(path=str(ROOT/'proof/simulation-mobile.png'),full_page=True)

    def test_ready_made_scenario_and_causal_trace(self):
        from playwright.sync_api import expect
        p = self.page
        p.get_by_role('button', name='Start a ready-made field station', exact=True).click()
        expect(p.locator('#agent-count')).to_have_text('3')
        expect(p.locator('#metric-pump')).to_have_text('broken')
        wid=self.app.store.overview()['workspaces'][0]['id']
        for _ in range(70):self.app.store.command(wid,'step',{})
        p.get_by_role('button',name='Inspect Sol',exact=True).click()
        p.get_by_role('button',name='Inspect journal').click()
        p.locator('#memory-entries').get_by_role('button',name='Trace record',exact=False).first.click()
        expect(p.locator('#memory-trace')).to_be_visible()
        expect(p.locator('#memory-trace')).to_contain_text('Source record')

    def test_versioned_assets_served(self):
        manifest=json.loads((ROOT/'art/manifest.json').read_text())
        for asset in manifest['assets']:
            response=self.page.request.get(self.url+'/assets/'+asset['path'])
            self.assertEqual(response.status,200,asset['path'])
            self.assertEqual(hashlib.sha256(response.body()).hexdigest(),asset['sha256'])

    def test_observer_reads_do_not_renew_lease(self):
        wid=self.app.store.create('Lease')
        self.app.running=wid;self.app.controller='absent';self.app.lease=0
        data=self.app.state(wid,'observer')
        self.assertFalse(data['running'])
        self.assertIsNone(self.app.controller)
