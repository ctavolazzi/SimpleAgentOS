import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import Store
from server import Application, ThreadingHTTPServer, handler


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        self.store = Store(self.path)
        self.wid = self.store.create('Test field')

    def agent(self):
        return self.store.command(self.wid, 'add_agent', {'name': 'Mira', 'personality': 'Curious and patient.', 'behavior': 'curious'})['agents'][0]

    def test_checkpoint_preserves_journal_and_state(self):
        a = self.agent()
        for _ in range(35):
            self.store.command(self.wid, 'step', {})
        before = self.store.read(self.wid)
        self.assertTrue(any(e['kind'] == 'observation' for e in before['entries']))
        saved = self.store.command(self.wid, 'checkpoint', {})
        cid = saved['checkpoints'][0]['id']
        if os.environ.get('WORKSPACE_MUTATION') == 'drop-journal':
            with self.store.connect() as db:
                payload = json.loads(db.execute('SELECT payload FROM checkpoints WHERE id=?', (cid,)).fetchone()[0])
                payload['entries'] = []
                db.execute('UPDATE checkpoints SET payload=? WHERE id=?', (json.dumps(payload), cid))
        for _ in range(10):
            self.store.command(self.wid, 'step', {})
        reopened = Store(self.path)
        restored = reopened.read(reopened.restore(cid))
        self.assertEqual(restored['agents'], before['agents'])
        self.assertEqual(restored['tick'], before['tick'])
        self.assertEqual([e['id'] for e in restored['entries']], [e['id'] for e in before['entries']])
        self.assertEqual(restored['agents'][0]['journal_id'], a['journal_id'])
        self.assertEqual(reopened.read(self.wid)['tick'], before['tick'] + 10)

    def test_journal_rejects_mutation(self):
        self.agent()
        for sql in ('UPDATE entries SET data=\'{}\'', 'DELETE FROM entries'):
            with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as db:
                db.execute(sql)
        self.assertEqual(self.store.read(self.wid)['entry_count'], 1)

    def test_invalid_agent_and_failed_entry_roll_back(self):
        with self.assertRaises(ValueError):
            self.store.command(self.wid, 'add_agent', {'name': '', 'personality': 'curious'})
        from unittest.mock import patch
        with patch.object(self.store, '_entry', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.agent()
        self.assertEqual(self.store.read(self.wid)['agents'], [])

    def test_profile_survives_workspace_branch_and_new_workspace(self):
        self.store.tutorial('active')
        a = self.agent()
        self.store.command(self.wid, 'inspect', {'agent_id': a['id']})
        self.store.command(self.wid, 'step', {})
        self.store.command(self.wid, 'checkpoint', {})
        other = self.store.create('Second field')
        self.assertEqual(Store(self.path).read(other)['profile']['tutorial'], 'complete')

    def test_edit_preserves_identity_and_records_revision(self):
        before = self.agent()
        after = self.store.command(self.wid, 'edit_agent', {'agent_id': before['id'], 'name': 'Mira II', 'personality': 'Methodical observer.'})
        self.assertEqual(after['agents'][0]['id'], before['id'])
        self.assertEqual(after['agents'][0]['journal_id'], before['journal_id'])
        self.assertEqual(after['agents'][0]['persona_revision'], 2)
        self.assertEqual(after['entry_count'], 2)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / 'browser.db')
        threading.Thread(target=self.app.tick, daemon=True).start()
        self.addCleanup(self.app.stop.set)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.external = []
        def route(r):
            if not r.request.url.startswith(self.url + '/'):
                self.external.append(r.request.url)
                r.abort()
            else:
                r.continue_()
        self.page.route('**/*', route)
        self.page.goto(self.url)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])

    def create_workspace(self):
        p = self.page
        p.locator('#create-workspace').click()
        p.get_by_label('Workspace name').fill('Browser field study')
        p.locator('#workspace-form').get_by_role('button', name='Create workspace', exact=True).click()

    def test_guided_create_observe_checkpoint_restore(self):
        from playwright.sync_api import expect
        p = self.page
        self.create_workspace()
        p.get_by_role('button', name='Show me around').click()
        expect(p.locator('#tutorial-title')).to_contain_text('1 of 4')
        p.locator('#add-agent').click()
        p.get_by_label('Agent name', exact=True).fill('Mira')
        p.locator('#agent-submit').click()
        expect(p.locator('#tutorial-title')).to_contain_text('2 of 4')
        p.get_by_role('button', name='Inspect Mira', exact=True).click()
        expect(p.locator('#tutorial-title')).to_contain_text('3 of 4')
        p.get_by_role('button', name='Step', exact=True).click()
        expect(p.locator('#journal')).to_contain_text('Heading to community garden.')
        expect(p.locator('#tutorial-title')).to_contain_text('4 of 4')
        p.get_by_role('button', name='Save checkpoint', exact=True).click()
        expect(p.locator('#tutorial')).to_be_hidden()
        p.reload()
        expect(p.locator('#run-state')).to_have_text('Paused')
        p.get_by_role('button', name='Inspect Mira', exact=True).click()
        expect(p.locator('#journal')).to_contain_text('Heading to community garden.')
        p.get_by_role('button', name='Step', exact=True).click()
        expect(p.locator('#world-clock')).to_have_text('Tick 2')
        p.get_by_role('button', name='Checkpoints', exact=True).click()
        p.get_by_role('button', name='Restore as new branch').click()
        expect(p.locator('#workspace-name')).to_contain_text('/ restored')
        expect(p.locator('#world-clock')).to_have_text('Tick 1')
        p.get_by_role('button', name='Inspect Mira', exact=True).click()
        expect(p.locator('#journal')).to_contain_text('Heading to community garden.')

    def test_mobile_skip_and_saved_workspace(self):
        from playwright.sync_api import expect
        p = self.page
        p.set_viewport_size({'width': 390, 'height': 844})
        self.create_workspace()
        p.get_by_role('button', name='Skip for now').click()
        p.locator('#back').click()
        p.locator('.save-card').click()
        expect(p.locator('#guide-dialog')).to_be_hidden()
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_resume_pause_and_reload(self):
        from playwright.sync_api import expect
        p = self.page
        self.create_workspace()
        p.get_by_role('button', name='Skip for now').click()
        p.get_by_role('button', name='Resume', exact=True).click()
        expect(p.locator('#run-state')).to_have_text('Running')
        expect(p.locator('#world-clock')).not_to_have_text('Tick 0')
        p.get_by_role('button', name='Pause', exact=True).click()
        expect(p.locator('#run-state')).to_have_text('Paused')
        wid = self.app.store.overview()['workspaces'][0]['id']
        tick = self.app.store.read(wid)['tick']
        time.sleep(.6)
        self.assertEqual(self.app.store.read(wid)['tick'], tick)
        p.get_by_role('button', name='Resume', exact=True).click()
        p.reload()
        expect(p.locator('#run-state')).to_have_text('Paused')


if __name__ == '__main__':
    unittest.main()
