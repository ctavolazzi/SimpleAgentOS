"""Negative controls for knowledge-dependent choices and control leases."""
import io
import unittest
from unittest.mock import patch
from test_simulation import SimulationTests, SimulationBrowserTests
import simulation
from server import Application


def expect_failure(case, mutation):
    with mutation:
        result=unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case]))
    if result.wasSuccessful() or result.errors:
        raise SystemExit(f'Invalid negative control: failures={len(result.failures)}, errors={len(result.errors)}')
    print('Observed expected assertion failure:',case.id())


choose=simulation.choose

def ignore_memory(agent,world):
    if agent['role']=='engineer':return 'inspect_pump', []
    return choose(agent,world)

expect_failure(SimulationTests('test_evidence_changes_engineer_choice'),patch.object(simulation,'choose',ignore_memory))

state=Application.state

def renew_on_read(self,wid,client=None):
    import time
    self.lease=time.monotonic()
    return state(self,wid,client)

expect_failure(SimulationBrowserTests('test_observer_reads_do_not_renew_lease'),patch.object(Application,'state',renew_on_read))
