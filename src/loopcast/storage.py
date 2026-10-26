"""Transactional task state. All execution and HTTP mutations pass through Store."""

from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .domain import STAGES, TERMINAL, uid, now, canonical, digest, Problem, actions, validate_input


from .ledger import PublicationMethods
from .studio_storage import StudioMethods
from .account_storage import AccountMethods
from .release_storage import ReleaseMethods
from .feedback_storage import FeedbackMethods


class Store(PublicationMethods, StudioMethods, AccountMethods, ReleaseMethods, FeedbackMethods):
    def __init__(self, directory, component=None):
        self.component = component
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = self.directory / 'artifacts'
        self.artifact_dir.mkdir(exist_ok=True)
        self.database = self.directory / 'state.sqlite3'
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            from .migrations import migrate
            migrate(db)

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
        allowed = actions(run['state'])
        if db.execute('SELECT 1 FROM publish_intents WHERE run_id=?', (run_id,)).fetchone():
            allowed['retry'] = {'enabled': False, 'reason_code': 'USE_TARGET_PUBLICATION_ACTIONS'}
        allowed['add_simulated_target'] = {'enabled': run['state'] == 'SUCCEEDED', 'reason_code': 'SIMULATOR_ONLY'}
        return {
            'run_id': run_id, 'mode': run['mode'], 'input': json.loads(run['input']),
            'input_digest': run['input_digest'], 'state': run['state'], 'state_version': run['version'],
            'created_at': run['created_at'], 'updated_at': run['updated_at'],
            'last_event_id': db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0],
            'tasks': [dict(x) for x in db.execute('SELECT id,stage,state,version FROM tasks WHERE run_id=? ORDER BY rowid', (run_id,))],
            'attempts': [dict(x) for x in db.execute('SELECT id,number,state,started_at,ended_at,error,evidence FROM attempts WHERE run_id=? ORDER BY number', (run_id,))],
            'artifacts': [dict(x) for x in db.execute('SELECT id,attempt_id,media_type,size,sha256 FROM artifacts WHERE run_id=? ORDER BY rowid', (run_id,))],
            'allowed_actions': allowed,
            'publications': self._publications(db, run_id),
            'component': self._component(db, run_id),
            'content_id': (dict(db.execute('SELECT id FROM contents WHERE run_id=?', (run_id,)).fetchone() or {}).get('id')),
            'media': [dict(x) for x in db.execute('SELECT m.* FROM media m JOIN artifacts a ON a.id=m.artifact_id WHERE a.run_id=?', (run_id,))],
        }

    def _component(self, db, run_id):
        row = db.execute('SELECT c.manifest FROM components c JOIN generation_context g ON c.id=g.component_id WHERE g.run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

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

    def _link_trace(self, db, trace, run_id, operation, intent_id=None, publication_attempt_id=None):
        if trace is None:
            return
        context = dict(trace) if isinstance(trace, dict) else {'trace_id': trace}
        trace_id = context.get('trace_id')
        if not isinstance(trace_id, str) or not re.fullmatch(r"[a-f0-9]{32}", trace_id) or trace_id == "0" * 32:
            raise Problem('INVALID_INPUT', 'Trace context is invalid.', 400, run_id)
        attempt = db.execute("SELECT id FROM attempts WHERE run_id=? ORDER BY number DESC LIMIT 1", (run_id,)).fetchone()
        db.execute('''INSERT INTO trace_links(id,trace_id,run_id,attempt_id,operation,created_at,
                   request_id,span_id,parent_span_id,traceparent,intent_id,publication_attempt_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (uid(), trace_id, run_id, attempt[0] if attempt else None, operation, now(),
                    context.get('request_id'), context.get('span_id'), context.get('parent_span_id'),
                    context.get('traceparent'), intent_id, publication_attempt_id))

    def _execution_trace(self, db, *, attempt_id=None, publication_attempt_id=None):
        # Use the originating mutation, never a later read/cancel/idempotent replay.
        if publication_attempt_id:
            where, key = "publication_attempt_id=? AND operation='publication.dispatch'", publication_attempt_id
        else:
            where, key = "attempt_id=? AND operation IN ('run.create','run.retry')", attempt_id
        row = db.execute('SELECT trace_id,request_id,span_id,parent_span_id,traceparent FROM trace_links WHERE ' + where + ' ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
        return dict(row) if row else {}

    def create(self, value, key, trace_id=None):
        with self.transaction(True) as db:
            return self.create_within(db, value, key, trace_id)

    def create_within(self, db, value, key, trace_id=None):
        value = validate_input(value)
        if value['mode'] == 'mpt':
            if self.component is None:
                raise Problem('CAPABILITY_UNAVAILABLE', 'Configure an independently installed generator first.', 422)
            value['component_id'] = digest(self.component)
        prior = self._reserve(db, 'create', key, value)
        if prior:
            self._link_trace(db, trace_id, prior['run_id'], 'create.replay')
            return prior, False
        run_id, time = uid(), now()
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (run_id, value['mode'], canonical(value), digest(value), 'QUEUED', 0, time, time, trace_id.get('trace_id') if isinstance(trace_id, dict) else trace_id))
        if value['mode'] == 'mpt':
            db.execute('INSERT OR IGNORE INTO components VALUES(?,?)', (value['component_id'], canonical(self.component)))
            db.execute('INSERT INTO generation_context VALUES(?,?)', (run_id, value['component_id']))
            db.execute('INSERT INTO contents VALUES(?,?)', (uid(), run_id))
        for stage in STAGES:
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (uid(), run_id, stage, 'QUEUED', 1))
        self._attempt(db, run_id)
        self._link_trace(db, trace_id, run_id, 'run.create')
        db.execute('UPDATE idempotency SET run_id=? WHERE scope=? AND key=?', (run_id, 'create', key))
        return self._event(db, run_id, 'created'), True

    def command(self, run_id, action, key, expected_version, trace_id=None):
        if action not in ('cancel', 'retry') or type(expected_version) is not int:
            raise Problem('INVALID_INPUT', 'Provide an action and integer expected_version.', 400, run_id)
        with self.transaction(True) as db:
            current = self._snapshot(db, run_id)
            scope = run_id + ':' + action
            prior = self._reserve(db, scope, key, {'expected_version': expected_version})
            if prior:
                self._link_trace(db, trace_id, run_id, action + '.replay')
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
            self._link_trace(db, trace_id, run_id, 'run.' + action)
            return self._event(db, run_id, action), True

    def claim(self):
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM attempts WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            attempt = dict(row)
            attempt['trace'] = self._execution_trace(db, attempt_id=attempt['id'])
            attempt['trace_id'] = attempt['trace'].get('trace_id')
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
            db.execute('UPDATE attempts SET state=?,ended_at=?,error=?,evidence=? WHERE id=?', (state, now(), error, 'bounded subprocess exited', attempt['id']))
            db.execute('UPDATE runs SET state=? WHERE id=?', (state, run_id))
            db.execute("UPDATE tasks SET state=?,version=version+1 WHERE run_id=? AND state IN ('QUEUED','RUNNING')", (state, run_id))
            if artifact:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)', (artifact['id'], run_id, attempt['id'], artifact.get('media_type', 'text/plain; charset=utf-8'), artifact['size'], artifact['sha256'], artifact['storage_ref']))
                if artifact.get('media'):
                    content_id = db.execute('SELECT id FROM contents WHERE run_id=?', (run_id,)).fetchone()[0]
                    db.execute('INSERT INTO media VALUES(?,?,?,?)', (artifact['id'], content_id, None, canonical(artifact['media'])))
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
