#!/usr/bin/env python3
"""Validate, traverse, and publish the SimpleAgent atlas. Standard library only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path.home() / 'Documents/Personal-Remote-Vault/Research/2026-09-20_simpleagent-map.html'


def validate(data):
    assert data['schema_version'] == 1, 'Unsupported schema'
    nodes = {n['id']: n for n in data['nodes']}
    assert len(nodes) == len(data['nodes']), 'Duplicate node ID'
    assert len({e['id'] for e in data['edges']}) == len(data['edges']), 'Duplicate edge ID'
    for n in nodes.values():
        assert n['status'] in ('observed', 'reproduced', 'proposed', 'unverified'), n['id']
        for key in ('id', 'label', 'kind', 'summary', 'detail'):
            assert isinstance(n[key], str) and n[key], (n['id'], key)
    for e in data['edges']:
        assert e['source'] in nodes and e['target'] in nodes, f'Dangling edge: {e["id"]}'
        assert e['kind'] in ('observed', 'proposed', 'unresolved'), e['id']
    assert len({v['id'] for v in data['views']}) == len(data['views']), 'Duplicate view ID'
    covered = set()
    for v in data['views']:
        assert set(v['positions']) <= nodes.keys(), f'Unknown view node: {v["id"]}'
        covered.update(v['positions'])
        for pos in v['positions'].values():
            assert len(pos) == 2 and all(isinstance(x, (int, float)) for x in pos), pos
    assert covered == nodes.keys(), 'Nodes missing from all views'
    return nodes


def walk(data, start, depth, direction):
    seen = {start}
    frontier = [start]
    output = []
    for level in range(depth + 1):
        output.append({'depth': level, 'nodes': frontier})
        following = []
        for node in frontier:
            for edge in data['edges']:
                neighbor = None
                if direction in ('out', 'both') and edge['source'] == node:
                    neighbor = edge['target']
                elif direction in ('in', 'both') and edge['target'] == node:
                    neighbor = edge['source']
                if neighbor and neighbor not in seen:
                    seen.add(neighbor)
                    following.append(neighbor)
        if not following:
            break
        frontier = following
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=HERE / 'graph.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate')
    sub.add_parser('check-sources')
    sub.add_parser('list')
    show = sub.add_parser('show'); show.add_argument('id')
    traversal = sub.add_parser('walk'); traversal.add_argument('id')
    traversal.add_argument('--depth', type=int, default=2)
    traversal.add_argument('--direction', choices=('in', 'out', 'both'), default='both')
    build = sub.add_parser('build'); build.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    data = json.loads(args.data.read_text())
    nodes = validate(data)
    if hasattr(args, 'id') and args.id not in nodes:
        parser.error('Unknown node ID: ' + args.id)
    if args.command == 'validate':
        print(f'Valid: {len(nodes)} nodes, {len(data["edges"])} edges, {len(data["views"])} views')
    elif args.command == 'list':
        print(json.dumps([{'id': n['id'], 'label': n['label'], 'status': n['status']} for n in nodes.values()], indent=2))
    elif args.command == 'show':
        print(json.dumps({'node': nodes[args.id], 'edges': [e for e in data['edges'] if args.id in (e['source'], e['target'])]}, indent=2))
    elif args.command == 'walk':
        if args.depth < 0:
            parser.error('Depth must be nonnegative')
        print(json.dumps(walk(data, args.id, args.depth, args.direction), indent=2))
    elif args.command == 'check-sources':
        stale = []
        for n in nodes.values():
            for e in n['evidence']:
                p = Path(e['path'])
                state = 'missing' if not p.exists() else 'unchanged' if hashlib.sha256(p.read_bytes()).hexdigest() == e['sha256'] else 'changed'
                if state != 'unchanged':
                    stale.append({'node': n['id'], 'path': str(p), 'state': state})
        print(json.dumps({'stale': stale, 'note': 'File hashes detect drift, not semantic correctness.'}, indent=2))
        return 1 if stale else 0
    elif args.command == 'build':
        # Escape HTML-sensitive characters so data cannot terminate its script block.
        payload = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        html = (HERE / 'template.html').read_text().replace('__GRAPH_DATA__', payload)
        target = args.output.expanduser().resolve()
        vault = (Path.home() / 'Documents/Personal-Remote-Vault').resolve()
        if target.is_relative_to(vault):
            sys.path.insert(0, str(HERE.parents[1]))
            import atomic_io
            with atomic_io.vault_lock():
                if target.exists():
                    target.read_text()  # Read existing artifact before replacing the generated view.
                atomic_io.atomic_write(target, html)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.read_text()
            target.write_text(html)
        assert target.read_text() == html, 'Read-back mismatch'
        print(target)
    return 0


if __name__ == '__main__':
    sys.exit(main())
