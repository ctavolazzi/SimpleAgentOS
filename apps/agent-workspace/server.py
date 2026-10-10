#!/usr/bin/env python3
"""Local-only application host. Run with --port 8787; no runtime dependencies."""
import argparse
import os
import logging
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from store import Store

WEB = Path(__file__).parent / 'web'


class Application:
    def __init__(self, db):
        self.store = Store(db)
        self.running = None
        self.lease = 0
        self.controller = None
        self.speed = 1
        self.lock = threading.RLock()
        self.error = None
        self.stop = threading.Event()
        self.telemetry_enabled = os.environ.get('FIELDWORK_WIRETAP') == '1'

    def forward_telemetry(self):
        from telemetry import deliver
        while not self.stop.wait(5):
            if self.telemetry_enabled:
                try:
                    self.store.flush_telemetry(deliver)
                except Exception:
                    logging.exception('Telemetry queue could not be processed')

    def incident(self, wid, kind, exc):
        logging.exception(kind)
        try:
            self.store.diagnostic(wid, kind, exception=type(exc).__name__)
        except Exception:
            logging.exception('Incident could not be persisted')

    def tick(self):
        while not self.stop.wait(.25):
            with self.lock:
                if self.running and time.monotonic() - self.lease > 5:
                    self.running = None
                if self.running:
                    try:
                        for _ in range(self.speed):
                            self.store.command(self.running, 'step', {})
                    except Exception as exc:
                        self.incident(self.running, 'simulation failed', exc)
                        self.running = None
                        self.error = 'Simulation paused after a storage error. See diagnostics.'

    def state(self, wid, client=None):
        with self.lock:
            if self.running and time.monotonic() - self.lease > 5:
                self.running = None
                self.controller = None
            return self.store.read(wid) | {'running': self.running == wid,
                'runtime_error': self.error, 'speed': self.speed,
                'can_control': self.controller in (None, client) or not self.running,
                'controller': self.controller == client and self.running == wid}


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, data, content_type='application/json'):
            raw = json.dumps(data).encode() if content_type == 'application/json' else data
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def trusted_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.trusted_host():
                return self.send(403, {'error': 'Use the local application address.'})
            path = urlparse(self.path)
            try:
                if path.path == '/api/workspaces':
                    self.send(200, app.store.overview())
                elif path.path == '/api/state':
                    q = parse_qs(path.query)
                    self.send(200, app.state(q['id'][0], q.get('client', [None])[0]))
                elif path.path == '/api/entry':
                    q = parse_qs(path.query)
                    self.send(200, app.store.entry(q['id'][0], q['entry'][0]))
                elif path.path == '/api/comparison':
                    q = parse_qs(path.query)
                    self.send(200, app.store.compare_checkpoint(q['id'][0], q.get('checkpoint', [None])[0]))
                elif path.path == '/api/journal/export':
                    q = parse_qs(path.query)
                    self.send(200, app.store.export_journal(q['id'][0], q['agent'][0]))
                elif path.path == '/api/journal':
                    q = parse_qs(path.query)
                    self.send(200, app.store.journal(q['id'][0], q['agent'][0], q.get('before', [None])[0], q.get('q', [''])[0]))
                elif path.path == '/api/diagnostics':
                    q = parse_qs(path.query)
                    self.send(200, app.store.diagnostics(q['id'][0]) | {'forwarding': app.telemetry_enabled})
                elif path.path == '/api/architecture':
                    self.send(200, json.loads((WEB.parent / 'architecture.json').read_text()))
                elif path.path.startswith('/assets/fieldwork-v2/') and path.path[len('/assets/fieldwork-v2/'):] in ('fieldwork-props.png', 'pump-flow.png', 'researcher-south.png', 'researcher-north.png', 'researcher-east.png', 'researcher-west.png', 'water-tank.png', 'researcher-walk-south.png', 'researcher-walk-east.png', 'researcher-walk-north.png', 'researcher-walk-west.png'):
                    self.send(200, (WEB / 'assets' / path.path[8:]).read_bytes(), 'image/png')
                elif path.path in ('/', '/app.js', '/world.js', '/style.css', '/insights.js', '/experiments.js'):
                    file = WEB / ('index.html' if path.path == '/' else path.path[1:])
                    self.send(200, file.read_bytes(), mimetypes.guess_type(file)[0] or 'text/plain')
                elif path.path == '/favicon.ico':
                    self.send(204, b'', 'image/x-icon')
                else:
                    self.send(404, {'error': 'Not found.'})
            except (ValueError, KeyError) as exc:
                self.send(400, {'error': str(exc)})
            except Exception as exc:
                app.incident(None, 'read failed', exc)
                self.send(500, {'error': 'Storage could not be read. Original data has been retained.'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            if not self.trusted_host() or self.headers.get('X-Workspace-Client') != '1' or (origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')):
                return self.send(403, {'error': 'Request must come from the local application.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768:
                    raise ValueError('Request size is invalid.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Expected an object.')
                with app.lock:
                    path = urlparse(self.path).path
                    client = body.get('client', 'legacy')
                    if not isinstance(client, str) or not 1 <= len(client) <= 80:
                        raise ValueError('Invalid controller identity.')
                    if app.running and time.monotonic() - app.lease > 5:
                        app.running = None; app.controller = None
                    if path == '/api/create':
                        if app.controller == client:
                            app.running = None; app.controller = None
                        result = {'id': app.store.create(body.get('name', ''))}
                    elif path == '/api/open':
                        app.store.read(body['id'])
                        if app.controller == client:
                            app.running = None; app.controller = None
                        result = app.state(body['id'], client)
                    elif path == '/api/run':
                        app.store.read(body['id'])
                        if app.running and app.controller != client and not body.get('takeover'):
                            raise ValueError('Another tab controls this simulation. Use Take control.')
                        speed = body.get('speed', app.speed)
                        if speed not in (1, 2, 4):
                            raise ValueError('Choose 1x, 2x or 4x speed.')
                        app.speed = speed
                        app.controller = client if body.get('running') else None
                        app.running = body['id'] if body.get('running') else None
                        app.lease = time.monotonic()
                        app.error = None
                        result = app.state(body['id'], client)
                    elif path == '/api/heartbeat':
                        if app.running == body['id'] and app.controller == client:
                            app.lease = time.monotonic()
                        result = app.state(body['id'], client)
                    elif path == '/api/command':
                        if app.running and app.controller != client and body.get('action') != 'inspect':
                            raise ValueError('Another tab controls this simulation. Use Take control.')
                        result = app.store.command(body['id'], body['action'], body)
                    elif path == '/api/tutorial':
                        app.store.tutorial(body['status'])
                        result = app.store.overview()['profile']
                    elif path == '/api/restore':
                        if app.running and app.controller != client:
                            raise ValueError('Another tab controls this simulation. Pause it or take control first.')
                        app.running = None; app.controller = None
                        result = {'id': app.store.restore(body['checkpoint_id'])}
                    else:
                        return self.send(404, {'error': 'Unknown command.'})
                self.send(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                if isinstance(locals().get('body'), dict) and isinstance(body.get('id'), str):
                    try:
                        app.store.diagnostic(body['id'], 'command rejected', level='warn', action=str(body.get('action', 'request'))[:40])
                    except Exception:
                        logging.exception('Could not record rejected command')
                self.send(400, {'error': str(exc)})
            except Exception as exc:
                app.incident(body.get('id') if isinstance(locals().get('body'), dict) else None, 'command failed', exc)
                self.send(500, {'error': 'The change could not be saved. Please retry; do not assume it succeeded.'})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--db', type=Path, default=Path.home() / 'Library/Application Support/SimpleAgentWorkspace/workspace.sqlite3')
    args = parser.parse_args()
    app = Application(args.db)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(app))
    threading.Thread(target=app.tick, daemon=True).start()
    threading.Thread(target=app.forward_telemetry, daemon=True).start()
    print(f'Agent workspace: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Data: {args.db}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
