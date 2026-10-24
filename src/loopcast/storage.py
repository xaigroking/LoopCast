"""Transactional task state. All execution and HTTP mutations pass through Store."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

STAGES = ('prepare', 'execute', 'report')
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Problem(Exception):
    def __init__(self, code, message, status=409, run_id=None):
        super().__init__(message)
        self.code, self.status, self.run_id = code, status, run_id


def actions(state):
    return {
        'cancel': {'enabled': state in {'QUEUED', 'RUNNING'},
                   'reason_code': 'AVAILABLE' if state in {'QUEUED', 'RUNNING'} else 'CANCEL_NOT_APPLICABLE'},
        'retry': {'enabled': state == 'FAILED',
                  'reason_code': 'AVAILABLE' if state == 'FAILED' else 'RETRY_REQUIRES_FAILURE'},
    }


def validate_input(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'title', 'scenario', 'delay_ms'}:
        raise Problem('INVALID_INPUT', 'Use mode, title, scenario and delay_ms fields only.', 400)
    if value.get('mode', 'fixture') != 'fixture':
        raise Problem('CAPABILITY_UNAVAILABLE', 'Only fixture mode is available.', 422)
    title = value.get('title', 'Untitled fixture')
    scenario = value.get('scenario', 'success')
    delay = value.get('delay_ms', 150)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise Problem('INVALID_INPUT', 'Title must contain 1–120 characters.', 400)
    if scenario not in ('success', 'fail_once', 'fail', 'slow'):
        raise Problem('INVALID_INPUT', 'Unknown fixture scenario.', 400)
    if type(delay) is not int or not 0 <= delay <= 3000:
        raise Problem('INVALID_INPUT', 'delay_ms must be an integer between 0 and 3000.', 400)
    return {'mode': 'fixture', 'title': title.strip(), 'scenario': scenario, 'delay_ms': delay}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = self.directory / 'artifacts'
        self.artifact_dir.mkdir(exist_ok=True)
        self.database = self.directory / 'state.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES (1);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode='fixture'),
                    input TEXT NOT NULL, input_digest TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    UNIQUE(run_id,stage));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    number INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                    error TEXT, evidence TEXT, UNIQUE(run_id,number));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    version INTEGER NOT NULL, type TEXT NOT NULL, time TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(id), media_type TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_ref TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id), PRIMARY KEY(scope,key));
            ''')
            if db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] != 1:
                raise RuntimeError('Unsupported state database version')

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self, write=False):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _snapshot(self, db, run_id):
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Task does not exist.', 404, run_id)
        run = dict(row)
        return {
            'run_id': run_id, 'mode': run['mode'], 'input': json.loads(run['input']),
            'input_digest': run['input_digest'], 'state': run['state'], 'state_version': run['version'],
            'created_at': run['created_at'], 'updated_at': run['updated_at'],
            'last_event_id': db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0],
            'tasks': [dict(x) for x in db.execute('SELECT id,stage,state,version FROM tasks WHERE run_id=? ORDER BY rowid', (run_id,))],
            'attempts': [dict(x) for x in db.execute('SELECT id,number,state,started_at,ended_at,error,evidence FROM attempts WHERE run_id=? ORDER BY number', (run_id,))],
            'artifacts': [dict(x) for x in db.execute('SELECT id,attempt_id,media_type,size,sha256 FROM artifacts WHERE run_id=? ORDER BY rowid', (run_id,))],
            'allowed_actions': actions(run['state']),
        }

    def snapshot(self, run_id):
        with self.transaction() as db:
            return self._snapshot(db, run_id)

    def list_runs(self, before=None, limit=30):
        with self.transaction() as db:
            rows = db.execute('SELECT id FROM runs WHERE rowid < COALESCE(?,9223372036854775807) ORDER BY rowid DESC LIMIT ?', (before, limit + 1)).fetchall()
            ids = [r[0] for r in rows[:limit]]
            cursor = db.execute('SELECT rowid FROM runs WHERE id=?', (ids[-1],)).fetchone()[0] if len(rows) > limit else None
            return {'items': [self._snapshot(db, r) for r in ids], 'next_cursor': cursor}

    def _event(self, db, run_id, kind):
        db.execute('UPDATE runs SET version=version+1, updated_at=? WHERE id=?', (now(), run_id))
        version = db.execute('SELECT version FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        cur = db.execute('INSERT INTO events(run_id,version,type,time,payload) VALUES(?,?,?,?,?)', (run_id, version, kind, now(), '{}'))
        snapshot = self._snapshot(db, run_id)
        db.execute('UPDATE events SET payload=? WHERE id=?', (canonical(snapshot), cur.lastrowid))
        return snapshot

    def _reserve(self, db, scope, key, payload):
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.isascii():
            raise Problem('IDEMPOTENCY_REQUIRED', 'Supply a 1–128 character Idempotency-Key.', 400)
        checksum = digest(payload)
        inserted = db.execute('INSERT INTO idempotency(scope,key,digest) VALUES(?,?,?) ON CONFLICT(scope,key) DO NOTHING', (scope, key, checksum)).rowcount
        if inserted:
            return None
        prior = db.execute('SELECT * FROM idempotency WHERE scope=? AND key=?', (scope, key)).fetchone()
        if prior['digest'] != checksum:
            raise Problem('IDEMPOTENCY_CONFLICT', 'This key was used with different input.', run_id=prior['run_id'])
        return self._snapshot(db, prior['run_id'])

    def _attempt(self, db, run_id):
        n = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM attempts WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('INSERT INTO attempts(id,run_id,number,token,state) VALUES(?,?,?,?,?)', (uid(), run_id, n, uid(), 'QUEUED'))

    def create(self, value, key):
        value = validate_input(value)
        with self.transaction(True) as db:
            prior = self._reserve(db, 'create', key, value)
            if prior:
                return prior, False
            run_id, time = uid(), now()
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (run_id, 'fixture', canonical(value), digest(value), 'QUEUED', 0, time, time))
            for stage in STAGES:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (uid(), run_id, stage, 'QUEUED', 1))
            self._attempt(db, run_id)
            db.execute('UPDATE idempotency SET run_id=? WHERE scope=? AND key=?', (run_id, 'create', key))
            return self._event(db, run_id, 'created'), True

    def command(self, run_id, action, key, expected_version):
        if action not in ('cancel', 'retry') or type(expected_version) is not int:
            raise Problem('INVALID_INPUT', 'Provide an action and integer expected_version.', 400, run_id)
        with self.transaction(True) as db:
            current = self._snapshot(db, run_id)
            scope = run_id + ':' + action
            prior = self._reserve(db, scope, key, {'expected_version': expected_version})
            if prior:
                return prior, False
            if expected_version != current['state_version']:
                raise Problem('VERSION_CONFLICT', 'State changed. Refresh and try again.', run_id=run_id)
            if not current['allowed_actions'][action]['enabled']:
                raise Problem('ACTION_UNAVAILABLE', 'This action is not available in the current state.', run_id=run_id)
            if action == 'retry':
                self._attempt(db, run_id)
                state = 'QUEUED'
                db.execute("UPDATE tasks SET state='QUEUED', version=version+1 WHERE run_id=?", (run_id,))
            elif current['state'] == 'QUEUED':
                state = 'CANCELLED'
                db.execute("UPDATE attempts SET state='CANCELLED', ended_at=? WHERE run_id=? AND state='QUEUED'", (now(), run_id))
                db.execute("UPDATE tasks SET state='CANCELLED', version=version+1 WHERE run_id=? AND state='QUEUED'", (run_id,))
            else:
                state = 'CANCEL_REQUESTED'
            db.execute('UPDATE runs SET state=? WHERE id=?', (state, run_id))
            db.execute('UPDATE idempotency SET run_id=? WHERE scope=? AND key=?', (run_id, scope, key))
            return self._event(db, run_id, action), True

    def claim(self):
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM attempts WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            attempt = dict(row)
            run_id = attempt['run_id']
            db.execute("UPDATE attempts SET state='RUNNING', started_at=? WHERE id=?", (now(), attempt['id']))
            db.execute("UPDATE runs SET state='RUNNING' WHERE id=?", (run_id,))
            self._event(db, run_id, 'started')
            attempt['input'] = json.loads(db.execute('SELECT input FROM runs WHERE id=?', (run_id,)).fetchone()[0])
            return attempt

    def _active(self, db, run_id, token):
        return db.execute("SELECT * FROM attempts WHERE run_id=? AND token=? AND state='RUNNING'", (run_id, token)).fetchone()

    def stage(self, run_id, token, stage, state):
        if stage not in STAGES or state not in ('RUNNING', 'SUCCEEDED'):
            raise ValueError('Invalid execution stage')
        with self.transaction(True) as db:
            if not self._active(db, run_id, token):
                return False
            db.execute('UPDATE tasks SET state=?, version=version+1 WHERE run_id=? AND stage=?', (state, run_id, stage))
            self._event(db, run_id, 'stage')
            return True

    def finish(self, run_id, token, state, error=None, artifact=None):
        if state not in TERMINAL:
            raise ValueError('Invalid terminal state')
        with self.transaction(True) as db:
            attempt = self._active(db, run_id, token)
            if not attempt:
                return False
            # Cancellation is acknowledged only after execution has exited. A completed
            # execution may still win a race with the cancellation request.
            db.execute('UPDATE attempts SET state=?,ended_at=?,error=?,evidence=? WHERE id=?', (state, now(), error, 'fixture subprocess exited', attempt['id']))
            db.execute('UPDATE runs SET state=? WHERE id=?', (state, run_id))
            db.execute("UPDATE tasks SET state=?,version=version+1 WHERE run_id=? AND state IN ('QUEUED','RUNNING')", (state, run_id))
            if artifact:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)', (artifact['id'], run_id, attempt['id'], 'text/plain; charset=utf-8', artifact['size'], artifact['sha256'], artifact['storage_ref']))
            self._event(db, run_id, 'finished')
            return True

    def recover(self):
        with self.transaction(True) as db:
            rows = db.execute("SELECT * FROM attempts WHERE state='RUNNING'").fetchall()
            for row in rows:
                db.execute("UPDATE attempts SET state='FAILED',ended_at=?,error='INTERRUPTED',evidence='Service restarted; old execution token retired' WHERE id=?", (now(), row['id']))
                db.execute("UPDATE runs SET state='FAILED' WHERE id=?", (row['run_id'],))
                db.execute("UPDATE tasks SET state='FAILED',version=version+1 WHERE run_id=? AND state IN ('QUEUED','RUNNING')", (row['run_id'],))
                self._event(db, row['run_id'], 'interrupted')

    def events(self, run_id, after):
        with self.transaction() as db:
            snapshot = self._snapshot(db, run_id)
            if type(after) is not int or after < 0 or after > snapshot['last_event_id']:
                raise Problem('RESYNC_REQUIRED', 'Fetch a new snapshot before reconnecting.', 409, run_id)
            if after and db.execute('SELECT 1 FROM events WHERE id=? AND run_id=?', (after, run_id)).fetchone() is None:
                raise Problem('RESYNC_REQUIRED', 'Cursor does not belong to this task.', 409, run_id)
            return [dict(x) for x in db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 100', (run_id, after))]

    def artifact(self, artifact_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
            if row is None:
                raise Problem('NOT_FOUND', 'Report is not registered or not ready.', 404)
            info = dict(row)
        path = self.artifact_dir / info['storage_ref']
        if path.is_symlink() or path.resolve().parent != self.artifact_dir.resolve():
            raise Problem('ARTIFACT_UNAVAILABLE', 'Report cannot be read safely.', 409, info['run_id'])
        try:
            data = path.read_bytes()
        except OSError:
            raise Problem('ARTIFACT_UNAVAILABLE', 'Report is missing from storage.', 409, info['run_id']) from None
        if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
            raise Problem('ARTIFACT_UNAVAILABLE', 'Report integrity check failed.', 409, info['run_id'])
        return info, data
