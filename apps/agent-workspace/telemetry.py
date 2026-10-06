"""Optional Wiretap delivery. The SQLite outbox remains authoritative.

No automatic retries of inference, no raw prompts, no journal bodies.
"""
import json
import urllib.request


def deliver(payload):
    request = urllib.request.Request('http://127.0.0.1:7778/log',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=.5) as response:
        if response.status != 200:
            raise RuntimeError('Wiretap did not acknowledge the event.')
