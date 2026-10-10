"""Browser CRUD contracts against isolated localStorage, never a live journal.

Run: python3 -m unittest discover -s tests/browser_journal -v
Negative control: CRUD_MUTATION=no-delete <same command>
"""
import json
import os
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'docs/journal-crud-fixture/index.html'
URL = 'https://journal.test/'
KEY = 'simpleagent.crud-fixture.v1'


class BrowserCRUD(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def context(self, **kwargs):
        context = self.browser.new_context(**kwargs)
        self.addCleanup(context.close)
        html = HTML.read_text()
        if os.environ.get('CRUD_MUTATION') == 'no-delete':
            original = '.entries.filter(r=>r.id!==selected)'
            assert original in html, 'Mutation target missing'
            html = html.replace(original, '.entries.filter(r=>true)')

        def route(request):
            if request.request.url == URL:
                request.fulfill(status=200, content_type='text/html', body=html)
            else:
                self.network.append(request.request.url)
                request.abort()
        context.route('**/*', route)
        context.on('page', lambda page: page.on('pageerror', lambda error: self.errors.append(str(error))))
        return context

    def setUp(self):
        self.errors = []
        self.network = []
        self.ctx = self.context()
        self.page = self.ctx.new_page()
        self.page.goto(URL)

    def tearDown(self):
        self.assertEqual(self.errors, [], 'Uncaught browser errors')
        self.assertEqual(self.network, [], 'Unexpected network request')

    def stored(self, page=None):
        return (page or self.page).evaluate('(key)=>JSON.parse(localStorage.getItem(key))', KEY)

    def create(self, title='First exchange', page=None, notes='Line one\n**Markdown**\n{"literal": true}'):
        page = page or self.page
        page.get_by_role('button', name='New record', exact=True).click()
        page.get_by_label('Title', exact=True).fill(title)
        page.get_by_label('Request', exact=True).fill('{"prompt":"hello"}')
        page.get_by_label('Response', exact=True).fill('Hello, 世界')
        page.get_by_label('Notes', exact=True).fill(notes)
        page.get_by_label('Metadata JSON').fill('{"fixture":true,"unknown":null}')
        page.get_by_role('button', name='Create record', exact=True).click()
        expect(page.get_by_role('status')).to_have_text('Record saved.')
        return json.loads(page.locator('#record-detail').inner_text())

    def crud_cycle(self, page):
        record = self.create(page=page)
        page.reload()
        page.locator('#records button').click()
        expect(page.get_by_label('Response', exact=True)).to_have_value('Hello, 世界')
        expect(page.get_by_label('Notes', exact=True)).to_have_value('Line one\n**Markdown**\n{"literal": true}')
        self.assertEqual(json.loads(page.locator('#record-detail').inner_text())['id'], record['id'])
        page.get_by_label('Notes', exact=True).fill('Changed notes')
        page.get_by_role('button', name='Save changes', exact=True).click()
        page.reload()
        page.locator('#records button').click()
        expect(page.get_by_label('Notes', exact=True)).to_have_value('Changed notes')
        updated = json.loads(page.locator('#record-detail').inner_text())
        self.assertEqual(updated['id'], record['id'])
        self.assertEqual(updated['createdAt'], record['createdAt'])
        self.assertEqual(updated['authorAgentId'], 'agent-a')
        page.once('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='Delete record', exact=True).click()
        expect(page.get_by_role('status')).to_have_text('Record deleted. Backup retained.')
        page.reload()
        expect(page.locator('#records')).to_have_text('No records in this journal.')
        state = self.stored(page)
        self.assertEqual(state['journals']['journal-a']['entries'], [])
        self.assertEqual(state['backups'][0]['record'], updated)

    def test_full_crud_desktop(self):
        self.crud_cycle(self.page)

    def test_full_crud_mobile(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.crud_cycle(self.page)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_cancel_edits_and_delete_preserves_record(self):
        record = self.create()
        before = self.stored()
        self.page.get_by_label('Notes', exact=True).fill('Unwanted change')
        self.page.get_by_role('button', name='Cancel edits').click()
        expect(self.page.get_by_label('Notes', exact=True)).to_have_value(record['notes'])
        self.page.once('dialog', lambda dialog: dialog.dismiss())
        self.page.get_by_role('button', name='Delete record', exact=True).click()
        self.assertEqual(self.stored(), before)

    def test_validation_does_not_partially_write(self):
        self.create()
        before = self.stored()
        for title, metadata, message in [('', '{}', 'Title is required.'), ('Valid', '{broken', 'Metadata must be valid JSON.')]:
            self.page.get_by_label('Title', exact=True).fill(title)
            self.page.get_by_label('Metadata JSON').fill(metadata)
            self.page.get_by_role('button', name='Save changes', exact=True).click()
            expect(self.page.get_by_role('alert')).to_have_text(message)
            self.assertEqual(self.stored(), before)
        self.page.get_by_label('Metadata JSON').fill('{}')
        self.page.get_by_label('Notes', exact=True).fill('x' * 65537)
        self.page.get_by_role('button', name='Save changes', exact=True).click()
        expect(self.page.get_by_role('alert')).to_contain_text('64 KiB')
        self.assertEqual(self.stored(), before)

    def test_journal_swap_author_identity_and_rehydration(self):
        first = self.create('Journal A record')
        self.page.get_by_label('Attached journal').select_option('journal-b')
        expect(self.page.locator('#records')).to_have_text('No records in this journal.')
        second = self.create('Journal B record')
        self.assertEqual(second['journalId'], 'journal-b')
        self.assertEqual(second['authorAgentId'], 'agent-a')
        self.page.get_by_label('Active agent').select_option('agent-b')
        self.page.locator('#records button').click()
        self.page.get_by_label('Notes', exact=True).fill('Edited by B')
        self.page.get_by_role('button', name='Save changes').click()
        changed = self.stored()['journals']['journal-b']['entries'][0]
        self.assertEqual(changed['authorAgentId'], 'agent-a')
        self.assertEqual(changed['updatedBy'], 'agent-b')
        # A fresh browser context with explicit persisted state, not process-restart durability.
        context = self.context(storage_state=self.ctx.storage_state())
        page = context.new_page(); page.goto(URL)
        expect(page.get_by_label('Attached journal')).to_have_value('journal-b')
        page.get_by_label('Attached journal').select_option('journal-a')
        page.locator('#records button').click()
        self.assertEqual(json.loads(page.locator('#record-detail').inner_text()), first)

    def test_storage_failure_does_not_claim_success_or_lose_record(self):
        self.create()
        before = self.stored()
        self.page.evaluate("() => { Storage.prototype.setItem = function(){throw new DOMException('Fixture quota exceeded','QuotaExceededError')}; }")
        self.page.get_by_label('Notes', exact=True).fill('Cannot persist')
        self.page.get_by_role('button', name='Save changes').click()
        expect(self.page.get_by_role('alert')).to_contain_text('quota exceeded')
        expect(self.page.get_by_role('status')).to_be_empty()
        self.assertEqual(self.stored(), before)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.get_by_role('button', name='Delete record', exact=True).click()
        self.assertEqual(self.stored(), before)
        expect(self.page.locator('#records button')).to_have_count(1)

    def test_corrupt_storage_retained_and_writes_disabled(self):
        self.page.evaluate('(key)=>localStorage.setItem(key,"{broken")', KEY)
        self.page.reload()
        expect(self.page.get_by_role('alert')).not_to_be_empty()
        expect(self.page.get_by_role('button', name='Create record', exact=True)).to_be_disabled()
        self.assertEqual(self.page.evaluate('(key)=>localStorage.getItem(key)', KEY), '{broken')

    def test_content_is_text_not_executable_html(self):
        payload = '<img src=x onerror="window.injected=true">'
        self.create(payload, notes='<script>window.injected=true</script>')
        self.page.reload(); self.page.locator('#records button').click()
        expect(self.page.get_by_label('Title', exact=True)).to_have_value(payload)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.assertEqual(self.page.locator('#records img, #record-detail script').count(), 0)

    def test_stale_tab_cannot_overwrite_newer_record(self):
        self.create()
        second = self.ctx.new_page(); second.goto(URL)
        second.locator('#records button').click()
        self.page.get_by_label('Notes', exact=True).fill('Newer value')
        self.page.get_by_role('button', name='Save changes').click()
        second.get_by_label('Notes', exact=True).fill('Stale value')
        second.get_by_role('button', name='Save changes').click()
        expect(second.get_by_role('alert')).to_contain_text('Reload stored data')
        self.assertEqual(self.stored()['journals']['journal-a']['entries'][0]['notes'], 'Newer value')


if __name__ == '__main__':
    unittest.main()
