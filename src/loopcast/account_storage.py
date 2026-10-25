"""Stable platform identities and owner-bound authorization sessions."""
import json
import re
import time
from .domain import Problem, canonical, now, uid

ACTIVE_AUTH = {'PENDING', 'WAITING', 'VERIFYING'}


class AccountMethods:
    def _account(self, db, identity):
        row = db.execute('SELECT * FROM studio_accounts WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Account does not exist.', 404)
        return {key: row[key] for key in ('id', 'platform', 'stable_subject', 'label', 'state', 'version', 'checked_at', 'evidence_mode')} | {'tags': json.loads(row['tags'])}

    def studio_accounts(self):
        with self.transaction() as db:
            return {'items': [self._account(db, row[0]) for row in db.execute('SELECT id FROM studio_accounts ORDER BY rowid DESC')]}

    def studio_private_account(self, identity):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM studio_accounts WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise Problem('NOT_FOUND', 'Account does not exist.', 404)
            return dict(row)

    def studio_account_update(self, identity, label, tags, expected_version, key, disconnect=False):
        request = {'label': label, 'tags': tags, 'expected_version': expected_version, 'disconnect': disconnect}
        scope = 'account.update:' + identity
        with self.transaction(True) as db:
            if self._studio_prior(db, scope, key, request):
                return self._account(db, identity)
            account = self._account(db, identity)
            if account['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the account.', 409)
            if disconnect:
                db.execute("UPDATE studio_accounts SET state='DISCONNECTED',credential_ref=NULL,version=version+1 WHERE id=?", (identity,))
                db.execute("UPDATE auth_sessions SET state='CANCELLED',version=version+1 WHERE account_id=? AND state IN ('PENDING','WAITING','VERIFYING')", (identity,))
            else:
                db.execute('UPDATE studio_accounts SET label=?,tags=?,version=version+1 WHERE id=?', (label, canonical(tags), identity))
            self._studio_remember(db, scope, key, request, {'id': identity})
            return self._account(db, identity)

    def _authorization(self, db, identity, owner_hash):
        row = db.execute('SELECT * FROM auth_sessions WHERE id=? AND owner_hash=?', (identity, owner_hash)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Authorization session does not exist.', 404)
        value = {key: row[key] for key in ('id', 'platform', 'account_id', 'method', 'state', 'expires_at', 'error_code', 'version')}
        if value['state'] in ACTIVE_AUTH and value['expires_at'] <= time.time():
            value['state'] = 'EXPIRED'
        return value

    def studio_authorization(self, identity, owner_hash):
        with self.transaction() as db:
            return self._authorization(db, identity, owner_hash)

    def studio_authorizations(self, owner_hash):
        with self.transaction() as db:
            return [self._authorization(db, r[0], owner_hash) for r in db.execute('SELECT id FROM auth_sessions WHERE owner_hash=? ORDER BY rowid DESC LIMIT 20', (owner_hash,))]

    def _studio_enqueue(self, db, kind, subject_id, deadline, trace=None, payload=None, token=None):
        if db.execute("SELECT COUNT(*) FROM studio_jobs WHERE state IN ('READY','RUNNING')").fetchone()[0] >= 32:
            raise Problem('QUEUE_FULL', 'The execution queue is full.', 429)
        identity, token = uid(), token or uid()
        db.execute('INSERT INTO studio_jobs VALUES(?,?,?,?,?,?,?,?)',
                   (identity, kind, subject_id, 'READY', token, deadline, canonical({'trace': trace or {}, 'payload': payload or {}}), now()))
        return token

    def studio_authorize(self, platform, account_id, method, owner_hash, key, trace=None, ttl=300):
        scope = 'authorization:' + owner_hash
        request = {'platform': platform, 'account_id': account_id, 'method': method}
        with self.transaction(True) as db:
            old = self._studio_prior(db, scope, key, request)
            if old:
                return self._authorization(db, old['id'], owner_hash)
            if account_id:
                account = self._account(db, account_id)
                if account['platform'] != platform:
                    raise Problem('INVALID_INPUT', 'Account platform does not match.', 400)
            active = db.execute("SELECT id FROM auth_sessions WHERE owner_hash=? AND platform=? AND COALESCE(account_id,'')=COALESCE(?,'') AND state IN ('PENDING','WAITING','VERIFYING') AND expires_at>?", (owner_hash, platform, account_id, time.time())).fetchone()
            if active:
                raise Problem('AUTHORIZATION_ACTIVE', 'Finish or cancel the existing session.', 409)
            identity, token = uid(), uid()
            expires = time.time() + ttl
            db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, owner_hash, platform, account_id, method, 'PENDING', expires, None, 1, token, now()))
            self._studio_enqueue(db, 'authorize', identity, expires, trace, token=token)
            self._studio_remember(db, scope, key, request, {'id': identity})
            return self._authorization(db, identity, owner_hash)

    def studio_cancel_authorization(self, identity, owner_hash, key):
        scope = 'authorization.cancel:' + identity
        with self.transaction(True) as db:
            session = self._authorization(db, identity, owner_hash)
            if self._studio_prior(db, scope, key, {}):
                return session
            if session['state'] in ACTIVE_AUTH:
                db.execute("UPDATE auth_sessions SET state='CANCELLED',version=version+1 WHERE id=?", (identity,))
            self._studio_remember(db, scope, key, {}, {'id': identity})
            return self._authorization(db, identity, owner_hash)

    def studio_check_account(self, identity, expected_version, key, trace=None):
        scope, request = 'account.check:' + identity, {'expected_version': expected_version}
        with self.transaction(True) as db:
            account = self._account(db, identity)
            if self._studio_prior(db, scope, key, request):
                return account
            if account['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the account.', 409)
            if account['state'] == 'DISCONNECTED':
                raise Problem('ACCOUNT_DISCONNECTED', 'Reconnect this account.', 409)
            db.execute("UPDATE studio_accounts SET state='PENDING',version=version+1 WHERE id=?", (identity,))
            self._studio_enqueue(db, 'check', identity, time.time() + 60, trace, {'account_version': expected_version + 1})
            self._studio_remember(db, scope, key, request, {'id': identity})
            return self._account(db, identity)

    def studio_claim_job(self):
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM studio_jobs WHERE state='READY' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            job = dict(row)
            if job['deadline'] <= time.time():
                db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE id=?", (job['id'],))
                if job['kind'] == 'authorize':
                    db.execute("UPDATE auth_sessions SET state='EXPIRED',version=version+1 WHERE id=? AND state IN ('PENDING','WAITING','VERIFYING')", (job['subject_id'],))
                elif job['kind'] == 'check':
                    db.execute("UPDATE studio_accounts SET state='EXPIRED',version=version+1 WHERE id=? AND state='PENDING'", (job['subject_id'],))
                elif job['kind'] == 'dispatch':
                    db.execute("UPDATE release_attempts SET state='NOT_SENT',ended_at=? WHERE token=? AND state='PREPARED'", (now(), job['token']))
                    db.execute("UPDATE release_targets SET state='NOT_SENT',version=version+1 WHERE id=? AND state='PREPARED'", (job['subject_id'],))
                    db.execute('DELETE FROM release_account_locks WHERE token=?', (job['token'],))
                return None
            if job['kind'] == 'authorize':
                session = db.execute('SELECT * FROM auth_sessions WHERE id=?', (job['subject_id'],)).fetchone()
                if session['state'] not in ACTIVE_AUTH:
                    db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE id=?", (job['id'],))
                    return None
                db.execute("UPDATE auth_sessions SET state='WAITING',version=version+1 WHERE id=?", (job['subject_id'],))
            db.execute("UPDATE studio_jobs SET state='RUNNING' WHERE id=?", (job['id'],))
            job.update(json.loads(job.pop('context')))
            return job

    def studio_job_active(self, job):
        with self.transaction() as db:
            row = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND token=? AND state='RUNNING' AND deadline>?", (job['id'], job['token'], time.time())).fetchone()
            if not row:
                return False
            if job['kind'] == 'authorize':
                row = db.execute('SELECT state,expires_at FROM auth_sessions WHERE id=? AND token=?', (job['subject_id'], job['token'])).fetchone()
                return bool(row and row['state'] in ACTIVE_AUTH and row['expires_at'] > time.time())
            return True

    def studio_authorization_job(self, identity):
        with self.transaction() as db:
            row = db.execute('SELECT platform,account_id,method,expires_at FROM auth_sessions WHERE id=?', (identity,)).fetchone()
            return dict(row) if row else None

    def studio_authorization_progress(self, job, state):
        if state not in {'WAITING', 'VERIFYING'}: return
        with self.transaction(True) as db:
            db.execute("UPDATE auth_sessions SET state=?,version=version+1 WHERE id=? AND token=? AND state IN ('PENDING','WAITING','VERIFYING') AND state!=? AND expires_at>?",
                       (state, job['subject_id'], job['token'], state, time.time()))

    def studio_complete_job(self, job):
        with self.transaction(True) as db:
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=? AND state='RUNNING'", (job['id'], job['token']))

    def studio_authorization_result(self, job, profile, credential_ref, evidence_mode):
        subject = profile.get('stable_subject') if isinstance(profile, dict) else None
        if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_:.@-]{1,128}', subject):
            return self.studio_fail_job(job, 'IDENTITY_UNVERIFIED')
        with self.transaction(True) as db:
            session = db.execute('SELECT * FROM auth_sessions WHERE id=? AND token=?', (job['subject_id'], job['token'])).fetchone()
            if session is None or session['state'] not in ACTIVE_AUTH or session['expires_at'] <= time.time():
                return False
            if session['account_id']:
                expected = self._account(db, session['account_id'])
                if expected['stable_subject'] != subject or expected['evidence_mode'] != evidence_mode:
                    db.execute("UPDATE auth_sessions SET state='FAILED',error_code='IDENTITY_CHANGED',version=version+1 WHERE id=?", (session['id'],))
                    db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
                    return False
            existing = db.execute('SELECT id FROM studio_accounts WHERE platform=? AND stable_subject=? AND evidence_mode=?', (session['platform'], subject, evidence_mode)).fetchone()
            identity = existing[0] if existing else uid()
            label = str(profile.get('label') or subject)[:120]
            if existing:
                db.execute("UPDATE studio_accounts SET credential_ref=?,state='AVAILABLE',checked_at=?,version=version+1 WHERE id=?", (credential_ref, now(), identity))
            else:
                db.execute('INSERT INTO studio_accounts VALUES(?,?,?,?,?,?,?,?,?,?)', (identity, session['platform'], subject, label, '[]', 'AVAILABLE', 1, credential_ref, now(), evidence_mode))
            db.execute("UPDATE auth_sessions SET state='SUCCEEDED',account_id=?,version=version+1 WHERE id=?", (identity, session['id']))
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
            return True

    def studio_check_result(self, job, valid, subject=None):
        with self.transaction(True) as db:
            row = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND token=? AND state='RUNNING'", (job['id'], job['token'])).fetchone()
            if not row:
                return False
            account = self._account(db, job['subject_id'])
            expected = job['payload']['account_version']
            if account['version'] == expected and account['state'] == 'PENDING':
                state = 'AVAILABLE' if valid and subject == account['stable_subject'] else 'EXPIRED'
                db.execute('UPDATE studio_accounts SET state=?,checked_at=?,version=version+1 WHERE id=?', (state, now(), job['subject_id']))
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
            return True

    def studio_fail_job(self, job, code):
        with self.transaction(True) as db:
            if job['kind'] == 'authorize':
                db.execute("UPDATE auth_sessions SET state=CASE WHEN expires_at<=? THEN 'EXPIRED' ELSE 'FAILED' END,error_code=?,version=version+1 WHERE id=? AND token=? AND state IN ('PENDING','WAITING','VERIFYING')", (time.time(), code, job['subject_id'], job['token']))
            elif job['kind'] == 'check':
                db.execute("UPDATE studio_accounts SET state='EXPIRED',version=version+1 WHERE id=? AND version=?", (job['subject_id'], job['payload']['account_version']))
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
            return False

    def studio_recover(self):
        with self.transaction(True) as db:
            db.execute("UPDATE auth_sessions SET state='FAILED',error_code='INTERRUPTED',version=version+1 WHERE id IN (SELECT subject_id FROM studio_jobs WHERE kind='authorize' AND state='RUNNING') AND state IN ('PENDING','WAITING','VERIFYING')")
            db.execute("UPDATE studio_accounts SET state='EXPIRED',version=version+1 WHERE state='PENDING'")
            for row in db.execute("SELECT * FROM release_attempts WHERE state IN ('PREPARED','SENDING')").fetchall():
                outcome = 'NOT_SENT' if row['state'] == 'PREPARED' else 'UNKNOWN'
                db.execute('UPDATE release_attempts SET state=? WHERE id=?', (outcome, row['id']))
                db.execute('UPDATE release_targets SET state=?,version=version+1 WHERE id=?', (outcome, row['target_id']))
                if outcome == 'NOT_SENT':
                    db.execute('DELETE FROM release_account_locks WHERE token=?', (row['token'],))
            db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE state='RUNNING' OR (state='READY' AND kind IN ('dispatch','verify'))")
