"""Regression contracts for reuse integrations; all storage is isolated."""
import copy
import http.client
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evidence import assess
import test_workspace as baseline


class IntegrationPersistenceTests(unittest.TestCase):
    setUp = baseline.PersistenceTests.setUp
    agent = baseline.PersistenceTests.agent

    def observation(self, agent):
        with self.store.connect() as db:
            return self.store._entry(db, self.store._load(db, self.wid), agent, 'observation', 'Garden needs water.')

    def test_empty_and_foreign_evidence_rejected_without_partial_write(self):
        a = self.agent()
        e = self.observation(a)
        other = self.store.command(self.wid, 'add_agent', {'name': 'Sol', 'personality': 'Patient'})['agents'][1]
        invalid = [[], ['missing'], [e['id'], 'missing'], [self.store.read(self.wid)['entries'][0]['id']]]
        before = self.store.read(self.wid)['entry_count']
        for ids in invalid:
            with self.assertRaises(ValueError):
                self.store.command(self.wid, 'decision', {'agent_id': a['id'], 'text': 'Water garden', 'evidence_ids': ids})
        with self.assertRaises(ValueError):
            self.store.command(self.wid, 'decision', {'agent_id': other['id'], 'text': 'Water garden', 'evidence_ids': [e['id']]})
        self.assertEqual(self.store.read(self.wid)['entry_count'], before)

    def test_supported_decision_and_checkpoint_keep_provenance(self):
        a = self.agent(); e = self.observation(a)
        state = self.store.command(self.wid, 'decision', {'agent_id': a['id'], 'text': 'Water garden', 'evidence_ids': [e['id']]})
        decision = state['entries'][0]
        self.assertEqual(decision['source'], 'human')
        self.assertEqual(decision['evidence_ids'], [e['id']])
        self.assertEqual(decision['actor_name'], 'You')
        self.assertEqual(decision['actor'], 'human')
        saved = self.store.command(self.wid, 'checkpoint', {})
        restored = self.store.read(self.store.restore(saved['checkpoints'][0]['id']))
        self.assertEqual(restored['entries'][0]['evidence_ids'], [e['id']])

    def test_quiet_agent_pagination_and_literal_search(self):
        a = self.agent()
        for _ in range(105): self.observation(a)
        other = self.store.command(self.wid, 'add_agent', {'name': 'Sol', 'personality': 'Patient'})['agents'][1]
        for _ in range(110): self.observation(other)
        page = self.store.journal(self.wid, a['id'])
        self.assertEqual(page['total'], 106)
        entries = page['items']
        while page['next'] is not None:
            page = self.store.journal(self.wid, a['id'], page['next'])
            entries += page['items']
        self.assertEqual(len({e['id'] for e in entries}), 106)
        self.assertTrue(all(e['author_agent_id'] == a['id'] for e in entries))
        self.assertEqual(self.store.journal(self.wid, a['id'], query='%')['total'], 0)
        self.assertEqual(self.store.journal(self.wid, a['id'], query='water')['total'], 105)

    def test_complete_journal_export_preserves_data_and_scope(self):
        a = self.agent()
        for _ in range(105): self.observation(a)
        other = self.store.command(self.wid, 'add_agent', {'name': 'Sol', 'personality': 'Patient'})['agents'][1]
        self.observation(other)
        exported = self.store.export_journal(self.wid, a['id'])
        self.assertEqual(exported['entry_count'], 106)
        self.assertEqual(len(exported['entries']), 106)
        self.assertEqual(exported['journal_id'], a['journal_id'])
        with self.store.connect() as db:
            expected = [json.loads(r[0]) for r in db.execute("SELECT data FROM entries WHERE workspace_id=? AND json_extract(data,'$.author_agent_id')=? ORDER BY seq", (self.wid, a['id']))]
        self.assertEqual(exported['entries'], expected)
        with self.assertRaises(ValueError):
            self.store.export_journal(self.store.create('Other workspace'), a['id'])

    def test_outbox_rejection_retained_and_concurrent_arrival_survives(self):
        self.store.diagnostic(self.wid, 'first')
        def reject(payload):
            raise urllib.error.HTTPError('http://invalid', 400, 'validation', {}, None)
        self.store.flush_telemetry(reject)
        self.assertEqual(self.store.diagnostics(self.wid)['pending'], 1)
        def deliver(payload):
            self.store.diagnostic(self.wid, 'arrived during upload')
        self.store.flush_telemetry(deliver)
        self.assertEqual(self.store.diagnostics(self.wid)['pending'], 1)
        self.store.flush_telemetry(lambda payload: None)
        self.assertEqual(self.store.diagnostics(self.wid)['pending'], 0)

    def test_outbox_recovers_after_reopen(self):
        from store import Store
        self.store.diagnostic(self.wid, 'restart')
        reopened = Store(self.path)
        self.assertEqual(reopened.diagnostics(self.wid)['pending'], 1)
        reopened.flush_telemetry(lambda payload: None)
        self.assertEqual(reopened.diagnostics(self.wid)['pending'], 0)

    def test_saved_places_drive_movement(self):
        a = self.agent()
        with self.store.connect() as db:
            s = self.store._load(db, self.wid)
            s['places'] = copy.deepcopy(s['places'])
            s['places'][0]['name'] = 'Saved garden'
            self.store._save(db, s)
        after = self.store.command(self.wid, 'step', {})
        self.assertIn('saved garden', after['entries'][0]['text'])

    def test_empty_population_gate(self):
        self.assertEqual(assess([], [])['status'], 'open')
        self.assertEqual(assess([], [])['evaluated'], 0)


class IntegrationBrowserTests(unittest.TestCase):
    setUp = baseline.BrowserTests.setUp
    tearDown = baseline.BrowserTests.tearDown
    create_workspace = baseline.BrowserTests.create_workspace

    def test_memory_decision_diagnostics_and_map(self):
        from playwright.sync_api import expect
        p = self.page
        self.create_workspace()
        p.get_by_role('button', name='Skip for now').click()
        wid = self.app.store.overview()['workspaces'][0]['id']
        a = self.app.store.command(wid, 'add_agent', {'name': 'Mira', 'personality': 'Patient'})['agents'][0]
        for _ in range(35): self.app.store.command(wid, 'step', {})
        p.get_by_role('button', name='Inspect Mira', exact=True).click()
        p.get_by_role('button', name='Inspect journal').click()
        expect(p.locator('#memory-status')).to_contain_text('matching records')
        p.get_by_text('Inspect stored entry JSON', exact=True).first.click()
        expect(p.locator('#memory-entries .stored-json').first).to_contain_text('journal_id')
        p.get_by_text('Inspect complete journal JSON', exact=True).click()
        expect(p.locator('#journal-json')).to_contain_text('fieldwork-journal-v1')
        with p.expect_download() as download:
            p.get_by_role('button', name='Download complete journal JSON').click()
        exported = json.loads(Path(download.value.path()).read_text())
        self.assertEqual(exported['entries'], self.app.store.export_journal(wid, a['id'])['entries'])
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/journal-inspector.png'))
        p.get_by_text('Inspect complete journal JSON', exact=True).click()
        p.get_by_label('Decision', exact=True).fill('Unsupported proposal')
        p.get_by_role('button', name='Save evidence-backed decision').click()
        expect(p.locator('#memory-dialog [role=alert]')).to_contain_text('Select at least one')
        p.get_by_label('Use as evidence').first.check()
        p.get_by_label('Decision', exact=True).fill('Visit the seedlings again tomorrow.')
        p.get_by_role('button', name='Save evidence-backed decision').click()
        expect(p.locator('#memory-entries')).to_contain_text('Visit the seedlings again tomorrow.')
        expect(p.locator('#memory-map')).to_contain_text('decision')
        p.locator('#memory-dialog').evaluate('(el) => el.scrollTop = 0')
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/memory.png'))
        p.locator('#memory-dialog [data-close]').click()
        p.get_by_role('button', name='Diagnostics', exact=True).click()
        expect(p.locator('#diagnostics-status')).to_contain_text('forwarding off')
        expect(p.locator('#diagnostics-list')).to_contain_text('decision')
        p.locator('#diagnostics-dialog [data-close]').click()
        p.get_by_role('button', name='System map', exact=True).click()
        expect(p.locator('#architecture-list')).to_contain_text('Evidence gate')
        p.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/integrations-mobile.png'))

    def test_http_guards_and_valid_control(self):
        port = self.server.server_port
        def request(path, method='GET', headers=None, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request(method, path, body, headers or {})
            response = conn.getresponse(); code = response.status; response.read(); conn.close()
            return code
        self.assertEqual(request('/api/workspaces'), 200)
        self.assertEqual(request('/api/workspaces', headers={'Host': 'evil.test'}), 403)
        self.assertEqual(request('/api/create', 'POST', {'Content-Type':'application/json'}, '{"name":"x"}'), 403)
        self.assertEqual(request('/api/create', 'POST', {'X-Workspace-Client':'1','Origin':'http://evil.test'}, '{"name":"x"}'), 403)
        self.assertEqual(request('/../store.py'), 404)
        self.assertEqual(request('/api/create', 'POST', {'X-Workspace-Client':'1'}, '{"name":"x"}'), 200)
