"""Permanent publication identities and conservative, fenced dispatch facts."""
import json
import re
import time
from uuid import uuid4
from datetime import datetime, timezone


def uid(): return uuid4().hex

def now(): return datetime.now(timezone.utc).isoformat()

def problem(code, message, status=409, run_id=None):
    from .storage import Problem
    return Problem(code, message, status, run_id)


def policy(state):
    return {'dispatch': {'enabled': state in ('CREATED', 'NOT_SENT'), 'reason_code': 'AVAILABLE' if state in ('CREATED', 'NOT_SENT') else 'PUBLICATION_NOT_RETRYABLE'},
            'verify': {'enabled': state == 'UNKNOWN', 'reason_code': 'AVAILABLE' if state == 'UNKNOWN' else 'NO_AMBIGUOUS_RESULT'}}


class PublicationMethods:
    def _publications(self, db, run_id):
        result = []
        for row in db.execute('SELECT * FROM publish_intents WHERE run_id=? ORDER BY rowid', (run_id,)):
            value = dict(row)
            value['mode'] = 'simulated'
            value['allowed_actions'] = policy(value['state'])
            value['attempts'] = [dict(x) for x in db.execute('SELECT id,state,started_at,ended_at FROM publish_attempts WHERE intent_id=? ORDER BY rowid', (row['id'],))]
            value['receipts'] = [dict(x) for x in db.execute('SELECT id,created_at,source,outcome,work_id,evidence FROM receipts WHERE intent_id=? ORDER BY rowid', (row['id'],))]
            result.append(value)
        return result

    def publication_create(self, run_id, value, key, expected_version):
        from .storage import digest, canonical
        if set(value) != {'platform', 'account'} or value['platform'] != 'simulator' or not isinstance(value['account'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', value['account']):
            raise problem('CAPABILITY_UNAVAILABLE', 'Only named simulator accounts are available.', 422, run_id)
        with self.transaction(True) as db:
            current = self._snapshot(db, run_id)
            scope = run_id + ':publication'
            old = db.execute('SELECT * FROM publish_requests WHERE scope=? AND key=?', (scope, key)).fetchone()
            checksum = digest({'value': value, 'expected_version': expected_version})
            if old:
                if old['digest'] != checksum: raise problem('IDEMPOTENCY_CONFLICT', 'Key already used.', run_id=run_id)
                return current
            self._publication_key(key)
            if type(expected_version) is not int or current['state_version'] != expected_version:
                raise problem('VERSION_CONFLICT', 'Refresh the task.', run_id=run_id)
            if current['state'] != 'SUCCEEDED' or not current['artifacts']:
                raise problem('ARTIFACT_UNAVAILABLE', 'Complete generation before creating publication intent.', run_id=run_id)
            content = db.execute('SELECT id FROM contents WHERE run_id=?', (run_id,)).fetchone()
            if content is None:
                content_id = uid()
                db.execute('INSERT INTO contents VALUES(?,?)', (content_id, run_id))
            else: content_id = content[0]
            identity = uid()
            db.execute('INSERT INTO publish_intents VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform,account,content_id) DO NOTHING',
                       (identity, run_id, content_id, value['platform'], value['account'], 'CREATED', 1, now()))
            identity = db.execute('SELECT id FROM publish_intents WHERE platform=? AND account=? AND content_id=?',
                                  (value['platform'], value['account'], content_id)).fetchone()[0]
            db.execute('INSERT INTO publish_requests VALUES(?,?,?,?)', (scope, key, checksum, identity))
            return self._event(db, run_id, 'publication_intent')

    @staticmethod
    def _publication_key(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.isascii():
            raise problem('IDEMPOTENCY_REQUIRED', 'Supply an Idempotency-Key.', 400)

    def publication_dispatch(self, run_id, intent_id, key, expected_version):
        from .storage import digest
        with self.transaction(True) as db:
            current = self._snapshot(db, run_id)
            self._publication_key(key)
            scope = intent_id + ':dispatch'
            checksum = digest({'expected_version': expected_version})
            old = db.execute('SELECT * FROM publish_requests WHERE scope=? AND key=?', (scope, key)).fetchone()
            if old:
                if old['digest'] != checksum: raise problem('IDEMPOTENCY_CONFLICT', 'Key already used.', run_id=run_id)
                return current, None
            row = db.execute('SELECT * FROM publish_intents WHERE id=? AND run_id=?', (intent_id, run_id)).fetchone()
            if row is None: raise problem('NOT_FOUND', 'Publication does not exist.', 404, run_id)
            if type(expected_version) is not int or expected_version != current['state_version']:
                raise problem('VERSION_CONFLICT', 'Refresh the task.', run_id=run_id)
            if not policy(row['state'])['dispatch']['enabled']:
                raise problem('PUBLICATION_NOT_RETRYABLE', 'Resolve publication evidence before another dispatch.', run_id=run_id)
            if db.execute('SELECT 1 FROM account_locks WHERE platform=? AND account=?', (row['platform'], row['account'])).fetchone():
                raise problem('ACCOUNT_BUSY', 'The account has an unresolved publisher.', run_id=run_id)
            token, attempt_id = uid(), uid()
            db.execute('INSERT INTO account_locks VALUES(?,?,?)', (row['platform'], row['account'], token))
            db.execute('INSERT INTO publish_attempts VALUES(?,?,?,?,?,?,?)', (attempt_id, intent_id, token, 'PREPARED', time.time()+30, now(), None))
            db.execute("UPDATE publish_intents SET state='PREPARED',version=version+1 WHERE id=?", (intent_id,))
            db.execute('INSERT INTO publish_requests VALUES(?,?,?,?)', (scope,key,checksum,intent_id))
            snapshot = self._event(db, run_id, 'publication_prepared')
            return snapshot, {'intent_id':intent_id,'run_id':run_id,'token':token,'attempt_id':attempt_id}

    def publication_sending(self, job):
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM publish_attempts WHERE token=? AND state='PREPARED' AND lease_until>?", (job['token'],time.time())).fetchone()
            if row is None: return False
            db.execute("UPDATE publish_attempts SET state='SENDING' WHERE token=?", (job['token'],))
            db.execute("UPDATE publish_intents SET state='SENDING',version=version+1 WHERE id=?", (row['intent_id'],))
            self._event(db, job['run_id'], 'publication_sending')
            return True

    def publication_result(self, job, outcome, work_id=None, evidence='No definitive receipt', source='simulator', exited=True):
        # Only the internal adapter calls this method. HTTP cannot submit platform assertions.
        if outcome not in {'PUBLISHED','NOT_SENT','UNKNOWN'}: raise ValueError('Invalid receipt outcome')
        if outcome == 'PUBLISHED' and (not isinstance(work_id,str) or not re.fullmatch('sim-[a-f0-9]{32}',work_id)):
            outcome, work_id = 'UNKNOWN', None
        if not exited: outcome,work_id = 'UNKNOWN',None
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM publish_attempts WHERE token=? AND state IN ('PREPARED','SENDING') AND lease_until>?", (job['token'],time.time())).fetchone()
            if row is None: return False
            if outcome == 'NOT_SENT' and row['state'] == 'SENDING':
                # A generic failure after the boundary never proves absence of sending.
                outcome = 'UNKNOWN'
            db.execute('UPDATE publish_attempts SET state=?,ended_at=? WHERE token=?', (outcome,now() if exited else None,job['token']))
            db.execute('UPDATE publish_intents SET state=?,version=version+1 WHERE id=?', (outcome,row['intent_id']))
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)', (uid(),row['intent_id'],row['id'],now(),source,outcome,work_id,evidence))
            if outcome != 'UNKNOWN': db.execute('DELETE FROM account_locks WHERE token=?', (job['token'],))
            self._event(db,job['run_id'],'publication_result')
            return True

    def publication_recover(self):
        with self.transaction(True) as db:
            rows = db.execute("SELECT a.*,i.run_id FROM publish_attempts a JOIN publish_intents i ON i.id=a.intent_id WHERE a.state IN ('PREPARED','SENDING')").fetchall()
            for row in rows:
                state = 'NOT_SENT' if row['state'] == 'PREPARED' else 'UNKNOWN'
                db.execute('UPDATE publish_attempts SET state=? WHERE id=?', (state,row['id']))
                db.execute('UPDATE publish_intents SET state=?,version=version+1 WHERE id=?', (state,row['intent_id']))
                if state == 'NOT_SENT': db.execute('DELETE FROM account_locks WHERE token=?', (row['token'],))
                self._event(db,row['run_id'],'publication_interrupted')

    def publication_verify(self,run_id,intent_id,key,expected_version):
        from .storage import digest
        with self.transaction(True) as db:
            current = self._snapshot(db,run_id)
            self._publication_key(key)
            scope,checksum = intent_id+':verify',digest({'expected_version':expected_version})
            old = db.execute('SELECT * FROM publish_requests WHERE scope=? AND key=?',(scope,key)).fetchone()
            if old:
                if old['digest'] != checksum: raise problem('IDEMPOTENCY_CONFLICT','Key already used.',run_id=run_id)
                return current
            row = db.execute('SELECT * FROM publish_intents WHERE id=? AND run_id=?',(intent_id,run_id)).fetchone()
            if row is None: raise problem('NOT_FOUND','Publication does not exist.',404,run_id)
            if type(expected_version) is not int or current['state_version'] != expected_version:
                raise problem('VERSION_CONFLICT','Refresh the task.',run_id=run_id)
            if row['state'] != 'UNKNOWN': raise problem('ACTION_UNAVAILABLE','No ambiguous result.',run_id=run_id)
            attempt = db.execute('SELECT * FROM publish_attempts WHERE intent_id=? ORDER BY rowid DESC LIMIT 1',(intent_id,)).fetchone()
            # Query the simulator's durable receipt. A missing record proves nothing.
            outcome,work_id,evidence = 'UNKNOWN',None,'No definitive simulator receipt; dispatch remains blocked'
            receipt_path = self.directory / 'simulator' / (attempt['id']+'.json')
            try:
                if receipt_path.is_symlink(): raise ValueError()
                receipt = json.loads(receipt_path.read_text())
                if set(receipt) != {'attempt_id','work_id'} or receipt['attempt_id'] != attempt['id'] or not re.fullmatch('sim-[a-f0-9]{32}',receipt['work_id']): raise ValueError()
                outcome,work_id,evidence = 'PUBLISHED',receipt['work_id'],'Durable simulator receipt matched dispatch identity'
            except (OSError,ValueError,TypeError): pass
            # Without an observed process exit we can record evidence, but cannot unlock the account.
            if outcome == 'PUBLISHED' and attempt['ended_at'] is None:
                outcome,work_id,evidence = 'UNKNOWN',None,'Receipt found; previous publisher exit remains unproven'
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',(uid(),intent_id,attempt['id'],now(),'simulator-query',outcome,work_id,evidence))
            if outcome == 'PUBLISHED':
                db.execute("UPDATE publish_intents SET state='PUBLISHED',version=version+1 WHERE id=?",(intent_id,))
                db.execute('DELETE FROM account_locks WHERE token=?',(attempt['token'],))
            db.execute('INSERT INTO publish_requests VALUES(?,?,?,?)',(scope,key,checksum,intent_id))
            return self._event(db,run_id,'publication_verified')
