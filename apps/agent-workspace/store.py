"""Checkpoint 1 domain and transactional persistence. No inference or paid APIs."""
import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from evidence import assess
import simulation

PLACES = [
    {'id': 'garden', 'name': 'Community garden', 'x': 70, 'y': 112, 'observation': 'Seedlings are growing beside the garden path.'},
    {'id': 'library', 'name': 'Field library', 'x': 162, 'y': 70, 'observation': 'A field guide describes the surrounding woodland.'},
    {'id': 'workshop', 'name': 'Workshop', 'x': 260, 'y': 117, 'observation': 'Tools and a half-finished birdhouse sit on the workbench.'},
]
COLORS = ['#cf734c', '#527caa', '#9972ad', '#bd943d', '#568a75', '#be6f8e']


def uid():
    return str(uuid.uuid4())


def title(value, limit=80):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Enter between 1 and {limit} characters.')
    return value.strip()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(workspace_id,id));
            CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, label TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS command_receipts(workspace_id TEXT NOT NULL, command_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(workspace_id,command_id));
            CREATE TABLE IF NOT EXISTS diagnostics(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS profile(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            INSERT OR IGNORE INTO profile VALUES(1,'{"tutorial":"new","step":0}');
            CREATE TRIGGER IF NOT EXISTS immutable_entry_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'Journal entries are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_entry_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'Journal entries are retained'); END;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _load(self, db, wid):
        row = db.execute('SELECT state FROM workspaces WHERE id=?', (wid,)).fetchone()
        if not row:
            raise ValueError('Workspace not found.')
        state = json.loads(row['state'])
        if state.get('version') != 1:
            raise ValueError('Unsupported workspace version. Original data retained.')
        return state

    def _save(self, db, state):
        state['revision'] += 1
        state['updated'] = time.time()
        db.execute('INSERT OR REPLACE INTO workspaces VALUES(?,?,?)', (state['id'], json.dumps(state), state['updated']))

    def _entry(self, db, state, agent, kind, text, source='local-rule', evidence_ids=None):
        entry = {'id': uid(), 'journal_id': agent['journal_id'], 'author_agent_id': agent['id'], 'persona_revision': agent['persona_revision'], 'tick': state['tick'], 'created': time.time(), 'kind': kind, 'text': text, 'source': source, 'actor': 'human' if source == 'human' else agent['id'], 'actor_name': 'You' if source == 'human' else agent['name'], 'evidence_ids': evidence_ids or [], 'workspace_id': state['id']}
        db.execute('INSERT INTO entries(workspace_id,id,data) VALUES(?,?,?)', (state['id'], entry['id'], json.dumps(entry)))
        return entry

    def _diagnostic(self, db, wid, kind, level='info', **extra):
        event_id = uid()
        payload = {'source': 'fieldwork', 'msg': kind, 'level': level,
                   'extra': {'event_id': event_id, 'workspace_id': wid,
                             'created': time.time(), **extra}}
        db.execute('INSERT INTO diagnostics(event_id,payload) VALUES(?,?)',
                   (event_id, json.dumps(payload)))

    def diagnostic(self, wid, kind, level='error', **extra):
        with self.lock, self.connect() as db:
            self._diagnostic(db, wid, kind, level, **extra)

    def diagnostics(self, wid):
        with self.lock, self.connect() as db:
            self._load(db, wid)
            rows = db.execute("SELECT * FROM diagnostics WHERE json_extract(payload,'$.extra.workspace_id')=? ORDER BY seq DESC LIMIT 30", (wid,)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM diagnostics WHERE delivered=0 AND json_extract(payload,'$.extra.workspace_id')=?", (wid,)).fetchone()[0]
            return {'pending': pending, 'events': [dict(r) | {'payload': json.loads(r['payload'])} for r in rows]}

    def flush_telemetry(self, deliver):
        # Snapshot IDs only; acknowledging one row never rewrites concurrent arrivals.
        with self.lock, self.connect() as db:
            batch = list(db.execute('SELECT event_id,payload FROM diagnostics WHERE delivered=0 ORDER BY seq LIMIT 20'))
        for row in batch:
            try:
                deliver(json.loads(row['payload']))
            except Exception as exc:
                with self.lock, self.connect() as db:
                    db.execute('UPDATE diagnostics SET attempts=attempts+1,error=? WHERE event_id=?',
                               (type(exc).__name__, row['event_id']))
            else:
                with self.lock, self.connect() as db:
                    db.execute('UPDATE diagnostics SET delivered=1,attempts=attempts+1,error=NULL WHERE event_id=?', (row['event_id'],))

    def entry(self, wid, entry_id):
        with self.lock, self.connect() as db:
            self._load(db, wid)
            row = db.execute('SELECT data FROM entries WHERE workspace_id=? AND id=?', (wid, entry_id)).fetchone()
            if not row:
                raise ValueError('Source record not found in this workspace.')
            return json.loads(row[0])

    def journal(self, wid, agent_id, before=None, query=''):
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('Search must be at most 200 characters.')
        with self.lock, self.connect() as db:
            state = self._load(db, wid)
            if not any(a['id'] == agent_id for a in state['agents']):
                raise ValueError('Agent not found.')
            clause = "workspace_id=? AND json_extract(data,'$.author_agent_id')=? AND instr(lower(json_extract(data,'$.text')),lower(?))>0"
            args = [wid, agent_id, query]
            total = db.execute('SELECT COUNT(*) FROM entries WHERE '+clause, args).fetchone()[0]
            if before is not None:
                clause += ' AND seq<?'
                args.append(int(before))
            rows = db.execute('SELECT seq,data FROM entries WHERE '+clause+' ORDER BY seq DESC LIMIT 41', args).fetchall()
            return {'total': total, 'items': [json.loads(r['data']) for r in rows[:40]],
                    'next': rows[39]['seq'] if len(rows) > 40 else None}

    def export_journal(self, wid, agent_id):
        """One consistent, complete snapshot, independent of search and page limits."""
        with self.lock, self.connect() as db:
            db.execute('BEGIN')
            state = self._load(db, wid)
            agent = next((a for a in state['agents'] if a['id'] == agent_id), None)
            if agent is None:
                raise ValueError('Agent not found.')
            rows = db.execute("SELECT data FROM entries WHERE workspace_id=? AND json_extract(data,'$.author_agent_id')=? ORDER BY seq", (wid, agent_id)).fetchall()
            return {'format': 'fieldwork-journal-v1', 'exported_at': time.time(),
                    'workspace_id': wid, 'workspace_name': state['name'],
                    'journal_id': agent['journal_id'], 'agent_id': agent_id,
                    'agent_name': agent['name'], 'entry_count': len(rows),
                    'entries': [json.loads(row['data']) for row in rows]}

    def compare_checkpoint(self, wid, checkpoint_id=None):
        """Compare one saved baseline with current state in the same read snapshot."""
        from comparison import compare
        with self.lock, self.connect() as db:
            db.execute('BEGIN')
            state = self._load(db, wid)
            parent_id = (state.get('parent') or {}).get('checkpoint_id')
            rows = db.execute('SELECT id,label,created,workspace_id FROM checkpoints WHERE workspace_id=? OR id=? ORDER BY created DESC', (wid, parent_id)).fetchall()
            options = [dict(row) for row in rows]
            if not options:
                if checkpoint_id:
                    raise ValueError('Checkpoint is not available for this workspace.')
                return {'checkpoints': [], 'comparison': None}
            selected = next((row for row in options if row['id'] == checkpoint_id), None) if checkpoint_id else options[0]
            if selected is None:
                raise ValueError('Checkpoint is not available for this workspace.')
            baseline = json.loads(db.execute('SELECT payload FROM checkpoints WHERE id=?', (selected['id'],)).fetchone()[0])
            entries = [json.loads(row[0]) for row in db.execute('SELECT data FROM entries WHERE workspace_id=? ORDER BY seq', (wid,))]
            return {'checkpoints': options, 'comparison': compare(baseline, state, entries) | {'checkpoint': selected, 'read_at': time.time()}}

    def _progress(self, db, expected):
        p = json.loads(db.execute('SELECT data FROM profile WHERE id=1').fetchone()[0])
        if p['tutorial'] == 'active' and p['step'] == expected:
            p['step'] += 1
            if p['step'] == 5:
                p['tutorial'] = 'complete'
            db.execute('UPDATE profile SET data=? WHERE id=1', (json.dumps(p),))

    def overview(self):
        with self.lock, self.connect() as db:
            spaces = []
            for row in db.execute('SELECT state FROM workspaces ORDER BY updated DESC'):
                s = json.loads(row[0])
                spaces.append({k: s[k] for k in ('id', 'name', 'updated', 'tick', 'parent') } | {'agents': len(s['agents'])})
            return {'workspaces': spaces, 'profile': json.loads(db.execute('SELECT data FROM profile').fetchone()[0])}

    def create(self, name):
        state = {'version': 1, 'id': uid(), 'name': title(name), 'revision': 0, 'tick': 0, 'updated': time.time(), 'parent': None, 'agents': [], 'places': PLACES, 'seed': 7}
        with self.lock, self.connect() as db:
            self._save(db, state)
        return state['id']

    def read(self, wid):
        with self.lock, self.connect() as db:
            state = self._load(db, wid)
            rows = db.execute('SELECT data FROM entries WHERE workspace_id=? ORDER BY seq DESC LIMIT 100', (wid,)).fetchall()
            state['entries'] = [json.loads(r[0]) for r in rows]
            state['entry_count'] = db.execute('SELECT COUNT(*) FROM entries WHERE workspace_id=?', (wid,)).fetchone()[0]
            state['checkpoints'] = [dict(r) for r in db.execute('SELECT id,label,created FROM checkpoints WHERE workspace_id=? ORDER BY created DESC', (wid,))]
            state['profile'] = json.loads(db.execute('SELECT data FROM profile').fetchone()[0])
            return state

    def command(self, wid, action, body):
        with self.lock, self.connect() as db:
            state = self._load(db, wid)
            command_id = body.get('command_id')
            digest = hashlib.sha256(json.dumps({'action': action, 'body': body}, sort_keys=True).encode()).hexdigest()
            if command_id:
                title(command_id, 80)
                receipt = db.execute('SELECT digest FROM command_receipts WHERE workspace_id=? AND command_id=?', (wid, command_id)).fetchone()
                if receipt:
                    if receipt[0] != digest:
                        raise ValueError('Command identifier was reused with different content.')
                    return self.read(wid)
            if 'expected_revision' in body and body['expected_revision'] != state['revision']:
                raise ValueError('Workspace changed. Refresh before retrying this action.')
            if action == 'add_agent':
                if len(state['agents']) >= 12:
                    raise ValueError('This checkpoint supports up to 12 agents.')
                personality = title(body.get('personality', ''), 1200)
                behavior = body.get('behavior', 'curious')
                if behavior not in ('curious', 'methodical', 'restful'):
                    raise ValueError('Unknown behavior.')
                a = {'id': uid(), 'name': title(body.get('name', ''), 40), 'personality': personality, 'behavior': behavior, 'persona_revision': 1, 'journal_id': uid(), 'x': 148 + len(state['agents']) * 3, 'y': 126, 'color': COLORS[len(state['agents']) % len(COLORS)], 'target': None, 'visits': 0, 'rest': 0, 'activity': 'Ready to explore', 'archived': False}
                state['agents'].append(a)
                self._entry(db, state, a, 'created', f'{a["name"]} arrived in the environment. Personality: {personality} Behavior: {behavior}.')
                self._progress(db, 1)
            elif action == 'inspect':
                if not any(a['id'] == body.get('agent_id') for a in state['agents']):
                    raise ValueError('Agent not found.')
                self._progress(db, 2)
            elif action == 'edit_agent':
                a = next((a for a in state['agents'] if a['id'] == body.get('agent_id')), None)
                if not a:
                    raise ValueError('Agent not found.')
                a['name'] = title(body.get('name', ''), 40)
                a['personality'] = title(body.get('personality', ''), 1200)
                a['persona_revision'] += 1
                self._entry(db, state, a, 'revised', f'Persona revision {a["persona_revision"]}: {a["personality"]}')
            elif action == 'start_simulation':
                if not state['agents']:
                    raise ValueError('Create at least one agent first.')
                simulation.initialize(state)
                for a in state['agents']:
                    self._entry(db, state, a, 'simulation', f"Living simulation started. Role: {a['role']}.", source='human')
            elif action == 'set_role':
                a = next((a for a in state['agents'] if a['id'] == body.get('agent_id')), None)
                if not a or not state.get('simulation'):
                    raise ValueError('Select an agent in a living simulation.')
                role = body.get('role')
                if role not in simulation.ROLES:
                    raise ValueError('Unknown role.')
                a['role'] = role; a['plan'] = None; a['working'] = 0
                a['persona_revision'] += 1
                self._entry(db, state, a, 'policy', 'Assigned role: '+role, source='human')
            elif action == 'intervene':
                sim = state.get('simulation')
                if not sim:
                    raise ValueError('Start the living simulation first.')
                intervention = body.get('intervention')
                if intervention == 'rain':
                    sim['weather'] = 'rain'; sim['weather_until'] = state['tick'] + 120
                elif intervention == 'drought':
                    sim['weather'] = 'clear'; sim['weather_until'] = 0; sim['moisture'] = 5
                elif intervention == 'break_pump':
                    sim['pump'] = 'broken'
                elif intervention == 'timber':
                    sim['wood'] += 3
                else:
                    raise ValueError('Unknown intervention.')
                for a in state['agents']:
                    self._entry(db, state, a, 'intervention', 'You changed the environment: '+intervention.replace('_', ' '), source='human')
            elif action == 'decision':
                a = next((a for a in state['agents'] if a['id'] == body.get('agent_id')), None)
                if not a:
                    raise ValueError('Agent not found.')
                proposal = title(body.get('text', ''), 1000)
                entries = [json.loads(r[0]) for r in db.execute(
                    "SELECT data FROM entries WHERE workspace_id=? AND json_extract(data,'$.author_agent_id')=?",
                    (wid, a['id']))]
                evaluation = assess(entries, body.get('evidence_ids', []))
                if evaluation['status'] != 'supported':
                    raise ValueError(evaluation['reason'])
                self._entry(db, state, a, 'decision', proposal, source='human',
                            evidence_ids=evaluation['evidence_ids'])
            elif action == 'step':
                self._step(db, state)
                if state['agents']:
                    self._progress(db, 3)
            elif action == 'checkpoint':
                label = title(body.get('label') or f'Checkpoint at tick {state["tick"]}')
                entries = [json.loads(r[0]) for r in db.execute('SELECT data FROM entries WHERE workspace_id=? ORDER BY seq', (wid,))]
                payload = {'state': state, 'entries': entries}
                db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)', (uid(), wid, label, time.time(), json.dumps(payload)))
                self._progress(db, 4)
            else:
                raise ValueError('Unknown command.')
            self._save(db, state)
            if command_id:
                db.execute('INSERT INTO command_receipts VALUES(?,?,?)', (wid, command_id, digest))
            if action != 'step' or state['tick'] % 20 == 0:
                self._diagnostic(db, wid, action, actor=body.get('agent_id', 'human'), revision=state['revision'])
        return self.read(wid)

    def _step(self, db, state):
        if state.get('simulation'):
            simulation.tick(state, lambda a, kind, text, ids: self._entry(db, state, a, kind, text, source='local-policy-v1', evidence_ids=ids))
            return
        state['tick'] += 1
        for index, a in enumerate(state['agents']):
            if a['archived']:
                continue
            if a['rest']:
                a['rest'] -= 1
                a['activity'] = 'Taking a moment'
                continue
            if a['target'] is None:
                # Deterministic policy: choice, routing, and progress survive save/restore.
                target_index = (a['visits'] + index) % len(state['places'])
                if a['behavior'] == 'curious':
                    target_index = (a['visits'] * 2 + index) % len(state['places'])
                a['target'] = target_index
                dest = state['places'][target_index]
                self._entry(db, state, a, 'intent', f'Heading to {dest["name"].lower()}.')
            dest = state['places'][a['target']]
            # All routes use the central footpath then its vertical branch.
            if a['x'] != dest['x']:
                if a['y'] != 126:
                    axis, goal = 'y', 126
                else:
                    axis, goal = 'x', dest['x']
            else:
                axis, goal = 'y', dest['y']
            delta = goal - a[axis]
            a[axis] += max(-5, min(5, delta))
            a['activity'] = 'Walking to ' + dest['name'].lower()
            if a['x'] == dest['x'] and a['y'] == dest['y']:
                self._entry(db, state, a, 'observation', dest['observation'])
                a['visits'] += 1
                a['target'] = None
                a['rest'] = 8 if a['behavior'] == 'restful' else 3
                a['activity'] = 'Observing ' + dest['name'].lower()

    def tutorial(self, value):
        if value not in ('active', 'skipped'):
            raise ValueError('Unknown tutorial action.')
        with self.lock, self.connect() as db:
            db.execute('UPDATE profile SET data=? WHERE id=1', (json.dumps({'tutorial': value, 'step': 1 if value == 'active' else 0}),))

    def restore(self, checkpoint_id):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT payload FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
            if not row:
                raise ValueError('Checkpoint not found.')
            payload = json.loads(row[0])
            state = copy.deepcopy(payload['state'])
            state['parent'] = {'workspace_id': state['id'], 'checkpoint_id': checkpoint_id}
            state['id'] = uid()
            state['name'] = state['name'][:60] + ' / restored'
            state['revision'] = 0
            self._save(db, state)
            for original in payload['entries']:
                entry = dict(original)
                entry['origin_workspace_id'] = entry.get('origin_workspace_id', entry['workspace_id'])
                entry['workspace_id'] = state['id']
                db.execute('INSERT INTO entries(workspace_id,id,data) VALUES(?,?,?)', (state['id'], entry['id'], json.dumps(entry)))
            return state['id']
