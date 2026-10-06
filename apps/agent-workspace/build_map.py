"""Refresh or check architecture source hashes after reviewing changed sources."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'architecture.json'
    data = json.loads(path.read_text())
    changed = []
    for item in data['nodes']:
        actual = hashlib.sha256((ROOT / item['path']).read_bytes()).hexdigest()
        if actual != item['sha256']:
            changed.append(item['path'])
        item['sha256'] = actual
    if args.check:
        print('Changed sources: ' + ', '.join(changed) if changed else 'Architecture sources match.')
        return bool(changed)
    path.write_text(json.dumps(data, indent=2) + '\n')
    print('Updated reviewed source hashes.')
    return False


if __name__ == '__main__':
    raise SystemExit(main())
