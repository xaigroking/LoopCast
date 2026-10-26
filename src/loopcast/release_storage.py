"""Frozen per-target releases and permanent identity/lineage protection."""
from datetime import datetime, timezone
import json
import time
from .domain import Problem, canonical, digest, now, uid
from .studio_rules import batch_state, classify_result

PROTECTED = {'READY', 'PREPARED', 'SENDING', 'SUBMITTED', 'PUBLISHED', 'UNKNOWN'}


class ReleaseMethods:
    def _release_blocker(self, db, account, family_id, exclude=None):
        families = self._family_members(db, family_id)
        marks = ','.join('?' for _ in families)
        rows = db.execute(f'''SELECT t.id,t.state FROM release_guards g JOIN release_targets t ON t.id=g.target_id
            WHERE g.platform=? AND g.stable_subject=? AND g.evidence_mode=? AND g.family_id IN ({marks})''',
                          (account['platform'], account['stable_subject'], account['evidence_mode'], *families))
        return next((r['state'] for r in rows if r['id'] != exclude and r['state'] in PROTECTED), None)

    def _preflight(self, db, value, capabilities, evidence_mode):
        if not value['title'].strip(): raise Problem('INVALID_INPUT', 'A title is required.', 400)
        asset = self._asset(db, value['asset_id'])
        if value.get('cover_id') and self._asset(db, value['cover_id'])['kind'] != 'image':
            raise Problem('INVALID_INPUT', 'A cover must be an image.', 400)
        accounts = [target['account_id'] for target in value['targets']]
        if len(set(accounts)) != len(accounts):
            raise Problem('INVALID_INPUT', 'Choose each account once.', 400)
        checks = []
        for target in value['targets']:
            account = self._account(db, target['account_id'])
            capability = capabilities[account['platform']]
            reasons = []
            if self._restore_guard(db): reasons.append('RESTORE_GUARD_ACTIVE')
            if asset['kind'] != 'video': reasons.append('VIDEO_REQUIRED')
            if not capability['configured'] or not capability['video']: reasons.append('CAPABILITY_UNAVAILABLE')
            if account['state'] != 'AVAILABLE': reasons.append('ACCOUNT_UNAVAILABLE')
            if account['evidence_mode'] != evidence_mode: reasons.append('EVIDENCE_MISMATCH')
            if not account['checked_at'] or (datetime.now(timezone.utc) - datetime.fromisoformat(account['checked_at'])).total_seconds() > 300:
                reasons.append('ACCOUNT_STALE')
            if len(value['title']) > capability['title_limit']: reasons.append('TITLE_TOO_LONG')
            if capability['requires_category'] and not target['fields'].get('category'): reasons.append('CATEGORY_REQUIRED')
            if not capability['requires_category'] and target['fields'].get('category') is not None: reasons.append('PLATFORM_FIELD_UNAVAILABLE')
            if target['fields']['visibility'] not in capability['visibility_options']: reasons.append('VISIBILITY_UNAVAILABLE')
            if target['fields'].get('declaration') and not capability['ai_disclosure']: reasons.append('AI_DISCLOSURE_UNAVAILABLE')
            if value.get('cover_id') and not capability['thumbnail']: reasons.append('COVER_UNAVAILABLE')
            blocker = self._release_blocker(db, account, asset['family_id'])
            if blocker: reasons.append('PERMANENT_DUPLICATE' if blocker == 'PUBLISHED' else 'PUBLICATION_UNRESOLVED')
            if db.execute('SELECT 1 FROM release_account_locks WHERE account_id=?', (account['id'],)).fetchone(): reasons.append('ACCOUNT_BUSY')
            checks.append({'account_id': account['id'], 'platform': account['platform'], 'allowed': not reasons,
                           'reasons': reasons, 'account_version': account['version']})
        checksum = digest({'input': value, 'checks': checks, 'asset_sha256': asset['sha256'], 'family': self._family_root(db, asset['family_id'])})
        return {'digest': checksum, 'allowed': all(c['allowed'] for c in checks), 'targets': checks, 'asset_sha256': asset['sha256']}

    def studio_preflight(self, value, capabilities, evidence_mode):
        with self.transaction() as db:
            return self._preflight(db, value, capabilities, evidence_mode)

    def _release_target(self, db, identity, query_platforms=()):
        row = db.execute('SELECT * FROM release_targets WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Release target does not exist.', 404)
        account = self._account(db, row['account_id'])
        blocked = self._release_blocker(db, account, row['family_id'], exclude=identity)
        value = {key: row[key] for key in ('id', 'account_id', 'platform', 'stable_subject', 'state', 'version', 'evidence_mode')}
        frozen = json.loads(row['frozen'])
        value.update({'fields': frozen['fields'], 'asset_sha256': frozen['asset_sha256']})
        value.update({
            'can_dispatch': row['state'] in {'READY', 'NOT_SENT'} and not blocked and account['state'] == 'AVAILABLE' and not self._restore_guard(db),
            'can_verify': row['state'] in {'UNKNOWN', 'SUBMITTED'} and row['platform'] in query_platforms,
            'can_cancel': row['state'] in {'READY', 'PREPARED', 'NOT_SENT'},
            'attempts': [dict(a) for a in db.execute('SELECT id,state,started_at,ended_at FROM release_attempts WHERE target_id=? ORDER BY rowid', (identity,))],
            'receipts': [dict(r) for r in db.execute('SELECT id,created_at,source,outcome,work_id,evidence_code FROM release_receipts WHERE target_id=? ORDER BY rowid', (identity,))],
        })
        if db.execute("SELECT 1 FROM studio_jobs WHERE subject_id=? AND kind='verify' AND state IN ('READY','RUNNING')", (identity,)).fetchone():
            value['can_verify'] = False
        return value

    def _release(self, db, identity, query_platforms=()):
        row = db.execute('SELECT * FROM release_batches WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Release batch does not exist.', 404)
        frozen = json.loads(row['frozen'])
        targets = [self._release_target(db, r[0], query_platforms) for r in db.execute('SELECT id FROM release_targets WHERE batch_id=? ORDER BY rowid', (identity,))]
        return {'id': identity, 'asset_id': row['asset_id'], 'title': frozen['title'], 'description': frozen['description'],
                'cover_id': frozen.get('cover_id'), 'input_digest': row['input_digest'], 'created_at': row['created_at'],
                'state': batch_state([t['state'] for t in targets]), 'targets': targets}

    def studio_releases(self, query_platforms=()):
        with self.transaction() as db:
            return {'items': [self._release(db, r[0], query_platforms) for r in db.execute('SELECT id FROM release_batches ORDER BY rowid DESC LIMIT 100')]}

    def studio_release(self, identity, query_platforms=()):
        with self.transaction() as db:
            return self._release(db, identity, query_platforms)

    def studio_create_release(self, value, preflight_digest, key, capabilities, evidence_mode):
        request = {'input': value, 'preflight_digest': preflight_digest}
        with self.transaction(True) as db:
            old = self._studio_prior(db, 'release.create', key, request)
            if old:
                return self._release(db, old['id'], [p for p, c in capabilities.items() if c['query']])
            check = self._preflight(db, value, capabilities, evidence_mode)
            if check['digest'] != preflight_digest:
                raise Problem('VERSION_CONFLICT', 'Run preflight again.', 409)
            if not check['allowed']:
                raise Problem('PREFLIGHT_BLOCKED', 'Resolve every target before confirming.', 409)
            identity = uid()
            db.execute('INSERT INTO release_batches VALUES(?,?,?,?,?)', (identity, value['asset_id'], canonical(value), digest(value), now()))
            asset = self._asset(db, value['asset_id'])
            family = self._family_root(db, asset['family_id'])
            for target in value['targets']:
                account = self._account(db, target['account_id'])
                target_id = uid()
                db.execute('INSERT INTO release_targets VALUES(?,?,?,?,?,?,?,?,?,?)', (target_id, identity, account['id'], account['platform'], account['stable_subject'], family,
                           canonical({'fields': target['fields'], 'asset_sha256': asset['sha256']}), 'READY', 1, evidence_mode))
                db.execute('INSERT INTO release_guards VALUES(?,?,?,?,?) ON CONFLICT(platform,stable_subject,family_id,evidence_mode) DO UPDATE SET target_id=excluded.target_id',
                           (account['platform'], account['stable_subject'], family, evidence_mode, target_id))
            self._studio_remember(db, 'release.create', key, request, {'id': identity})
            return self._release(db, identity, [p for p, c in capabilities.items() if c['query']])

    def studio_dispatch(self, batch_id, target_id, expected_version, key, capabilities, sending_enabled, trace=None, timeout=300):
        scope, request = 'release.dispatch:' + target_id, {'expected_version': expected_version}
        with self.transaction(True) as db:
            self._require_upload(db)
            old = self._studio_prior(db, scope, key, request)
            if old:
                return self._release(db, batch_id, [p for p, c in capabilities.items() if c['query']])
            row = db.execute('SELECT * FROM release_targets WHERE id=? AND batch_id=?', (target_id, batch_id)).fetchone()
            if row is None:
                raise Problem('NOT_FOUND', 'Release target does not exist.', 404)
            if row['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the release target.', 409)
            account = self._account(db, row['account_id'])
            if not sending_enabled:
                raise Problem('SENDING_DISABLED', 'Sending is disabled by service configuration.', 409)
            if row['state'] not in {'READY', 'NOT_SENT'} or self._release_blocker(db, account, row['family_id'], exclude=target_id):
                raise Problem('PUBLICATION_NOT_RETRYABLE', 'Resolve earlier publication evidence.', 409)
            if account['state'] != 'AVAILABLE' or not capabilities[row['platform']]['configured']:
                raise Problem('ACCOUNT_UNAVAILABLE', 'Verify the account first.', 409)
            if not account['checked_at'] or (datetime.now(timezone.utc) - datetime.fromisoformat(account['checked_at'])).total_seconds() > 300:
                raise Problem('ACCOUNT_STALE', 'Verify the account again.', 409)
            if db.execute('SELECT 1 FROM release_account_locks WHERE account_id=?', (account['id'],)).fetchone():
                raise Problem('ACCOUNT_BUSY', 'The account has an unresolved attempt.', 409)
            attempt_id, token = uid(), uid()
            deadline = time.time() + timeout
            db.execute('INSERT INTO release_account_locks VALUES(?,?)', (account['id'], token))
            db.execute('INSERT INTO release_attempts VALUES(?,?,?,?,?,?,?)', (attempt_id, target_id, token, 'PREPARED', deadline + 15, now(), None))
            db.execute("UPDATE release_targets SET state='PREPARED',version=version+1 WHERE id=?", (target_id,))
            self._studio_enqueue(db, 'dispatch', target_id, deadline, trace, {'attempt_id': attempt_id}, token=token)
            self._studio_remember(db, scope, key, request, {'id': target_id})
            return self._release(db, batch_id, [p for p, c in capabilities.items() if c['query']])

    def studio_release_job(self, job):
        with self.transaction() as db:
            if not self._job_epoch_current(db, job):raise Problem('EXECUTOR_UNRESOLVED', 'The execution identity is no longer current.', 409)
            if job['kind'] == 'dispatch':self._require_upload(db)
            target = db.execute('SELECT * FROM release_targets WHERE id=?', (job['subject_id'],)).fetchone()
            if target is None:
                raise Problem('NOT_FOUND', 'Release target does not exist.', 404)
            batch = db.execute('SELECT * FROM release_batches WHERE id=?', (target['batch_id'],)).fetchone()
            return dict(target) | {'batch': json.loads(batch['frozen']), 'fields': json.loads(target['frozen'])['fields'],
                                   'account': dict(db.execute('SELECT * FROM studio_accounts WHERE id=?', (target['account_id'],)).fetchone())}

    def studio_sending(self, job):
        with self.transaction(True) as db:
            if self._restore_guard(db) or not self._job_epoch_current(db, job):return False
            row = db.execute("SELECT a.*,t.account_id,t.family_id FROM release_attempts a JOIN release_targets t ON t.id=a.target_id WHERE a.token=? AND a.state='PREPARED' AND a.lease_until>?", (job['token'], time.time())).fetchone()
            if row is None:
                return False
            account = self._account(db, row['account_id'])
            if account['state'] != 'AVAILABLE' or self._release_blocker(db, account, row['family_id'], exclude=row['target_id']):
                return False
            db.execute("UPDATE release_attempts SET state='SENDING' WHERE token=?", (job['token'],))
            db.execute("UPDATE release_targets SET state='SENDING',version=version+1 WHERE id=?", (job['subject_id'],))
            return True

    def studio_release_result(self, job, result, source):
        outcome, work_id, evidence_code = classify_result(result)
        with self.transaction(True) as db:
            if self._restore_guard(db) or not self._job_epoch_current(db, job):return False
            row = db.execute("SELECT * FROM release_attempts WHERE token=? AND state IN ('PREPARED','SENDING') AND lease_until>?", (job['token'], time.time())).fetchone()
            if row is None:
                return False
            # Work identifiers and definitive assertions must come from the execution
            # adapter. There is no HTTP endpoint for inventing receipts or clearing guards.
            db.execute('UPDATE release_attempts SET state=?,ended_at=? WHERE token=?', (outcome, now(), job['token']))
            db.execute('UPDATE release_targets SET state=?,version=version+1 WHERE id=?', (outcome, job['subject_id']))
            db.execute('INSERT INTO release_receipts VALUES(?,?,?,?,?,?,?,?)', (uid(), job['subject_id'], row['id'], now(), source, outcome, work_id, evidence_code))
            if outcome in {'PUBLISHED', 'NOT_SENT'}:
                db.execute('DELETE FROM release_account_locks WHERE token=?', (job['token'],))
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
            return True

    def studio_verify(self, batch_id, target_id, expected_version, key, query_platforms, trace=None):
        scope, request = 'release.verify:' + target_id, {'expected_version': expected_version}
        with self.transaction(True) as db:
            target = db.execute('SELECT * FROM release_targets WHERE id=? AND batch_id=?', (target_id, batch_id)).fetchone()
            if target is None: raise Problem('NOT_FOUND', 'Release target does not exist.', 404)
            if self._studio_prior(db, scope, key, request): return self._release(db, batch_id, query_platforms)
            if target['platform'] not in query_platforms: raise Problem('QUERY_UNAVAILABLE', 'This adapter cannot query publication results.', 409)
            if target['version'] != expected_version: raise Problem('VERSION_CONFLICT', 'Refresh the release target.', 409)
            if target['state'] not in {'SUBMITTED', 'UNKNOWN'}: raise Problem('ACTION_UNAVAILABLE', 'No unresolved result.', 409)
            attempt = db.execute('SELECT * FROM release_attempts WHERE target_id=? ORDER BY rowid DESC LIMIT 1', (target_id,)).fetchone()
            if attempt['ended_at'] is None:
                raise Problem('EXECUTOR_UNRESOLVED', 'Previous publisher exit is not proven.', 409)
            self._studio_enqueue(db, 'verify', target_id, time.time() + 60, trace, {'attempt_id': attempt['id']})
            db.execute('UPDATE release_targets SET version=version+1 WHERE id=?', (target_id,))
            self._studio_remember(db, scope, key, request, {'id': target_id})
            return self._release(db, batch_id, query_platforms)

    def studio_verification_result(self, job, result, source):
        outcome, work_id, evidence_code = classify_result(result)
        with self.transaction(True) as db:
            if not self._job_epoch_current(db, job):return False
            queued = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND token=? AND state='RUNNING'", (job['id'], job['token'])).fetchone()
            if queued is None: return False
            attempt = db.execute('SELECT * FROM release_attempts WHERE id=? AND target_id=?', (job['payload']['attempt_id'], job['subject_id'])).fetchone()
            target = db.execute('SELECT * FROM release_targets WHERE id=?', (job['subject_id'],)).fetchone()
            if target['state'] not in {'SUBMITTED', 'UNKNOWN'} or attempt is None or attempt['ended_at'] is None: return False
            # A read-only lookup may establish publication; absence proves nothing.
            if outcome != 'PUBLISHED': outcome, work_id, evidence_code = 'UNKNOWN', None, 'NO_DEFINITIVE_RECEIPT'
            db.execute('INSERT INTO release_receipts VALUES(?,?,?,?,?,?,?,?)', (uid(), target['id'], attempt['id'], now(), source, outcome, work_id, evidence_code))
            if outcome == 'PUBLISHED':
                db.execute("UPDATE release_targets SET state='PUBLISHED',version=version+1 WHERE id=?", (target['id'],))
                db.execute('DELETE FROM release_account_locks WHERE token=?', (attempt['token'],))
            else:
                db.execute('UPDATE release_targets SET version=version+1 WHERE id=?', (target['id'],))
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=?", (job['id'], job['token']))
            return True

    def studio_cancel_release(self, batch_id, target_id, expected_version, key, query_platforms):
        scope, request = 'release.cancel:' + target_id, {'expected_version': expected_version}
        with self.transaction(True) as db:
            target = db.execute('SELECT * FROM release_targets WHERE id=? AND batch_id=?', (target_id, batch_id)).fetchone()
            if target is None: raise Problem('NOT_FOUND', 'Release target does not exist.', 404)
            if self._studio_prior(db, scope, key, request): return self._release(db, batch_id, query_platforms)
            if target['version'] != expected_version: raise Problem('VERSION_CONFLICT', 'Refresh the release target.', 409)
            if target['state'] not in {'READY', 'PREPARED', 'NOT_SENT'}: raise Problem('ACTION_UNAVAILABLE', 'An active or unresolved send cannot be cancelled as unsent.', 409)
            for attempt in db.execute("SELECT token FROM release_attempts WHERE target_id=? AND state='PREPARED'", (target_id,)).fetchall():
                db.execute("UPDATE release_attempts SET state='CANCELLED',ended_at=? WHERE token=?", (now(), attempt['token']))
                db.execute('DELETE FROM release_account_locks WHERE token=?', (attempt['token'],))
            db.execute("UPDATE release_targets SET state='CANCELLED',version=version+1 WHERE id=?", (target_id,))
            db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE subject_id=? AND kind='dispatch' AND state='READY'", (target_id,))
            self._studio_remember(db, scope, key, request, {'id': target_id})
            return self._release(db, batch_id, query_platforms)
