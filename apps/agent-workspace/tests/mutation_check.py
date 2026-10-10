"""In-memory negative controls. No production files or databases are mutated."""
import io
import unittest
from unittest.mock import patch
import test_integrations as tests
import store


def run_case(name, replacement):
    case = tests.IntegrationPersistenceTests(name)
    with replacement:
        result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case]))
    if result.wasSuccessful() or result.errors:
        raise SystemExit(f'Negative control invalid: {name}, failures={len(result.failures)}, errors={len(result.errors)}')
    print('Observed expected assertion failure:', name)


def discard_on_failure(self, deliver):
    with self.connect() as db:
        db.execute('UPDATE diagnostics SET delivered=1')


run_case('test_empty_and_foreign_evidence_rejected_without_partial_write',
         patch.object(store, 'assess', return_value={'status': 'supported', 'evidence_ids': []}))
run_case('test_outbox_rejection_retained_and_concurrent_arrival_survives',
         patch.object(store.Store, 'flush_telemetry', discard_on_failure))

original_export = store.Store.export_journal

def truncated_export(self, wid, agent_id):
    data = original_export(self, wid, agent_id)
    data['entries'] = data['entries'][:40]
    return data

run_case('test_complete_journal_export_preserves_data_and_scope',
         patch.object(store.Store, 'export_journal', truncated_export))

import test_checkpoint3
original_compare = store.Store.compare_checkpoint

def omit_same_tick_records(self, wid, checkpoint_id=None):
    data = original_compare(self, wid, checkpoint_id)
    data['comparison']['new_entry_count'] = 0
    return data

with patch.object(store.Store, 'compare_checkpoint', omit_same_tick_records):
    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([
        test_checkpoint3.ComparisonTests('test_same_tick_intervention_and_branch_baseline')]))
if result.wasSuccessful() or result.errors:
    raise SystemExit('Same-tick comparison negative control invalid')
print('Observed expected assertion failure: same-tick intervention omitted')
