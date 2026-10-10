"""Small deterministic evidence gate, inspired by Claimground and Sightglass.

Evaluates recorded observations, not source truth or model intelligence.
"""


def assess(entries, evidence_ids):
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 20:
        raise ValueError('Choose up to 20 observations.')
    if not all(isinstance(item, str) for item in evidence_ids):
        raise ValueError('Evidence identifiers must be text.')
    requested = list(dict.fromkeys(evidence_ids))
    by_id = {e['id']: e for e in entries}
    missing = [eid for eid in requested if eid not in by_id]
    invalid = [eid for eid in requested if eid in by_id and by_id[eid]['kind'] != 'observation']
    count = len(requested) - len(missing) - len(invalid)
    return {'status': 'supported' if count and not missing and not invalid else 'open',
            'evaluated': count, 'missing': missing, 'invalid': invalid,
            'evidence_ids': requested,
            'reason': 'Recorded observations attached; relevance requires human review.' if count and not missing and not invalid else 'Select at least one recorded observation from this agent journal.'}
