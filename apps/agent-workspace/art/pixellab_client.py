"""Explicit development-time PixelLab calls. Never used by the simulation server.

Reads the existing local MCP configuration; credentials are never copied to assets.
"""
import json
import urllib.request
from pathlib import Path


def call(name, arguments):
    config = json.loads((Path.home() / '.claude.json').read_text())['mcpServers']['pixellab']
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': arguments}}
    request = urllib.request.Request(config['url'], data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', **config.get('headers', {})})
    with urllib.request.urlopen(request, timeout=45) as response:
        raw = response.read().decode()
    if raw.lstrip().startswith('{'):
        result = json.loads(raw)
    else:
        result = next(json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: '))
    if 'error' in result:
        raise RuntimeError(result['error'])
    return result['result']


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('tool')
    parser.add_argument('arguments', help='JSON object')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new output path to preserve earlier job receipts.')
    result = call(args.tool, json.loads(args.arguments))
    args.output.write_text(json.dumps(result, indent=2))
    for item in result.get('content', []):
        if item.get('type') == 'text':
            print(item['text'])
