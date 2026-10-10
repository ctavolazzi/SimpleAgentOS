"""Experiment review contracts, isolated from user workspaces."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_workspace as baseline


class ComparisonTests(unittest.TestCase):
    setUp = baseline.PersistenceTests.setUp
    agent = baseline.PersistenceTests.agent

    def test_same_tick_intervention_and_branch_baseline(self):
        self.agent()
        self.store.command(self.wid, 'start_simulation', {})
        saved = self.store.command(self.wid, 'checkpoint', {'label': 'Before timber'})
        checkpoint = saved['checkpoints'][0]['id']
        branch = self.store.restore(checkpoint)
        self.store.command(branch, 'intervene', {'intervention': 'timber'})
        before = self.store.read(branch)
        data = self.store.compare_checkpoint(branch, checkpoint)['comparison']
        timber = next(m for m in data['metrics'] if m['label'] == 'Timber')
        self.assertEqual((timber['before'], timber['after'], timber['delta']), (3, 6, 3))
        self.assertEqual(data['baseline_tick'], data['current_tick'])
        self.assertEqual(data['new_entry_count'], 1)
        self.assertEqual(data['recent_entries'][0]['kind'], 'intervention')
        self.assertEqual(data['recent_entries'][0]['workspace_id'], branch)
        self.assertEqual(self.store.read(branch), before)
        self.assertEqual(self.store.read(self.wid)['simulation']['wood'], 3)
        self.assertEqual(self.store.compare_checkpoint(branch)['comparison']['checkpoint']['id'], checkpoint)

    def test_empty_foreign_checkpoint_and_long_history(self):
        self.agent()
        self.assertIsNone(self.store.compare_checkpoint(self.wid)['comparison'])
        saved = self.store.command(self.wid, 'checkpoint', {})
        checkpoint = saved['checkpoints'][0]['id']
        other = self.store.create('Unrelated')
        with self.assertRaises(ValueError): self.store.compare_checkpoint(other, checkpoint)
        with self.assertRaises(ValueError): self.store.compare_checkpoint(self.wid, 'missing')
        for i in range(110):
            self.store.command(self.wid, 'edit_agent', {'agent_id': saved['agents'][0]['id'], 'name': 'Mira', 'personality': str(i)})
        data = self.store.compare_checkpoint(self.wid)['comparison']
        self.assertEqual(data['new_entry_count'], 110)
        self.assertEqual(data['event_counts'], {'revised': 110})
        self.assertEqual(len(data['recent_entries']), 20)
        self.assertEqual(data['agents'][0]['new_entries'], 110)


class ComparisonBrowserTests(unittest.TestCase):
    setUp = baseline.BrowserTests.setUp
    tearDown = baseline.BrowserTests.tearDown

    def test_named_baseline_comparison_download_and_mobile_focus(self):
        from playwright.sync_api import expect
        p = self.page
        p.get_by_role('button', name='Start a ready-made field station').click()
        p.get_by_role('button', name='Compare checkpoint', exact=True).click()
        expect(p.locator('#comparison-status')).to_contain_text('No baseline yet')
        p.locator('#comparison-dialog [data-close]').click()
        p.get_by_role('button', name='Checkpoints', exact=True).click()
        p.get_by_label('Name this checkpoint').fill('Before timber')
        p.get_by_role('button', name='Save named checkpoint').click()
        expect(p.locator('#comparison-status')).to_contain_text('0 new journal records')
        p.locator('#comparison-dialog [data-close]').click()
        p.get_by_label('Change the environment').select_option('timber')
        p.get_by_role('button', name='Apply intervention').click()
        p.get_by_role('button', name='Compare checkpoint', exact=True).click()
        row = p.get_by_role('row').filter(has=p.get_by_role('rowheader', name='Timber', exact=True))
        expect(row).to_contain_text('+3')
        expect(p.locator('#comparison-status')).to_contain_text('3 new journal records')
        with p.expect_download() as download:
            p.get_by_role('button', name='Download comparison JSON').click()
        exported = json.loads(Path(download.value.path()).read_text())
        self.assertEqual(exported['new_entry_count'], 3)
        self.assertEqual(exported['checkpoint']['label'], 'Before timber')
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/checkpoint-3-comparison.png'))
        p.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(p.locator('#comparison-dialog').evaluate('(el) => el.scrollWidth <= el.clientWidth'))
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/checkpoint-3-comparison-mobile.png'))
        p.locator('#comparison-dialog [data-close]').click()
        p.get_by_role('link', name='Agents', exact=True).click()
        p.get_by_role('button', name='Inspect Mira', exact=True).click()
        expect(p.locator('#agent-inspector')).to_be_focused()
        p.get_by_role('button', name='Inspect journal', exact=True).click()
        expect(p.locator('#memory-title')).to_contain_text('Mira')
        p.locator('#memory-dialog [data-close]').click()
        p.get_by_role('link', name='World', exact=True).click()
        self.assertIn('workspace=', p.url)
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        p.screenshot(path=str(Path(__file__).resolve().parents[1] / 'proof/checkpoint-3-mobile.png'))
        p.set_viewport_size({'width': 1440, 'height': 1000})
        agent = p.get_by_role('button', name='Inspect Mira', exact=True)
        agent.focus()
        p.wait_for_timeout(1100)
        expect(agent).to_be_focused()
