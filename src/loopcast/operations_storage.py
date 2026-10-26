"""Durable controlled-restore boundaries, fencing and read-only reconciliation."""
import hashlib
import json
from contextlib import contextmanager
from .domain import Problem, canonical, now, uid


class OperationsMethods:
    def _execution_epoch(self, db):
        return db.execute('SELECT execution_epoch FROM operations_state WHERE id=1').fetchone()[0]

    def _restore_guard(self, db):
        row = db.execute('SELECT * FROM restore_guard WHERE id=1').fetchone()
        return dict(row) if row else None

    def _require_upload(self, db):
        if self._restore_guard(db):
            raise Problem('RESTORE_GUARD_ACTIVE', 'Controlled recovery requires reliable publication reconciliation. Uploads remain blocked.', 409)

    def _job_epoch_current(self, db, job):
        row = db.execute("SELECT context FROM studio_jobs WHERE id=? AND token=? AND state='RUNNING'", (job['id'], job['token'])).fetchone()
        if row is None:
            return False
        epoch = self._execution_epoch(db)
        # Missing epochs are allowed only for pre-upgrade work before any restore.
        stored = json.loads(row['context']).get('execution_epoch')
        supplied = job.get('execution_epoch')
        return stored == supplied == epoch or stored is None and supplied is None and not self._restore_guard(db)

    def studio_require_upload(self, job):
        with self.transaction() as db:
            self._require_upload(db)
            if not self._job_epoch_current(db, job):
                raise Problem('EXECUTOR_UNRESOLVED', 'The execution identity is no longer current.', 409)

    def prepare_restored_state(self, manifest, manifest_sha256):
        with self.transaction(True) as db:
            identity, epoch, restored = uid(), uid(), now()
            db.execute('INSERT INTO restore_events VALUES(?,?,?,?,?,?,?,?)',
                       (identity, manifest['backup_id'], manifest_sha256, manifest['created_at'], restored, manifest['schema_version'], epoch, 'ALL_STUDIO_UPLOADS'))
            db.execute('UPDATE operations_state SET execution_epoch=? WHERE id=1', (epoch,))
            db.execute("INSERT INTO restore_guard VALUES(1,?,1,'ACTIVE','EXTERNAL_HISTORY_UNRECONCILED') ON CONFLICT(id) DO UPDATE SET restore_id=excluded.restore_id,version=restore_guard.version+1,state='ACTIVE',reason=excluded.reason", (identity,))
            def fence(kind, item, token):
                db.execute('INSERT INTO restore_fences VALUES(?,?,?,?)', (identity, kind, item, hashlib.sha256(token.encode()).hexdigest()))
            for row in db.execute("SELECT * FROM release_attempts WHERE state IN ('PREPARED','SENDING')").fetchall():
                fence('release_attempt', row['id'], row['token']); token = uid()
                db.execute("UPDATE release_attempts SET token=?,state='UNKNOWN',ended_at=? WHERE id=?", (token, restored, row['id']))
                db.execute("UPDATE release_targets SET state='UNKNOWN',version=version+1 WHERE id=?", (row['target_id'],))
                db.execute('UPDATE release_account_locks SET token=? WHERE token=?', (token, row['token']))
                db.execute('INSERT INTO release_receipts VALUES(?,?,?,?,?,?,?,?)',
                           (uid(), row['target_id'], row['id'], restored, 'control:restore', 'UNKNOWN', None, 'RESTORE_BOUNDARY_UNKNOWN'))
            for row in db.execute("SELECT * FROM studio_jobs WHERE state IN ('READY','RUNNING')").fetchall():
                fence('studio_job', row['id'], row['token'])
                context = json.loads(row['context']); context['execution_epoch'] = epoch
                db.execute("UPDATE studio_jobs SET token=?,state='CANCELLED',context=? WHERE id=?", (uid(), canonical(context), row['id']))
            for row in db.execute("SELECT id,token FROM auth_sessions WHERE state IN ('PENDING','WAITING','VERIFYING')").fetchall():
                fence('authorization', row['id'], row['token'])
                db.execute("UPDATE auth_sessions SET token=?,state='EXPIRED',error_code='RESTORE_REAUTH_REQUIRED',version=version+1 WHERE id=?", (uid(), row['id']))
            db.execute("UPDATE studio_accounts SET credential_ref=NULL,state='DISCONNECTED',version=version+1")
            return self._operations_status(db)

    def _operations_status(self, db):
        guard = self._restore_guard(db)
        event = dict(db.execute('SELECT * FROM restore_events WHERE id=?', (guard['restore_id'],)).fetchone()) if guard else None
        last = db.execute('SELECT * FROM backup_audit ORDER BY created_at DESC,id DESC LIMIT 1').fetchone()
        return {'restore_guard': guard, 'restore': event, 'backup': dict(last) if last else None,
                'upload_allowed_by_recovery': guard is None, 'release_supported': False}

    def operations_status(self):
        with self.transaction() as db:
            return self._operations_status(db)

    def operations_reconciliation(self, query_platforms=()):
        with self.transaction() as db:
            status = self._operations_status(db)
            targets = []
            for row in db.execute('SELECT * FROM release_targets ORDER BY id'):
                receipts = [dict(r) for r in db.execute('SELECT id,source,outcome,work_id,evidence_code FROM release_receipts WHERE target_id=? ORDER BY rowid', (row['id'],))]
                confirmed = [r for r in receipts if r['outcome'] == 'PUBLISHED' and r['work_id'] and r['evidence_code'] == 'CONFIRMED_WORK']
                targets.append({'target_id': row['id'], 'account_id': row['account_id'], 'platform': row['platform'],
                                'stable_subject': row['stable_subject'], 'evidence_mode': row['evidence_mode'], 'state': row['state'],
                                'known_publication': 'FOUND' if confirmed else 'INCONCLUSIVE',
                                'query': 'available' if row['platform'] in query_platforms else 'unavailable', 'receipts': receipts})
            return {'format': 'loopcast.reconciliation.v1', 'guard': status['restore_guard'], 'restore': status['restore'],
                    'scope': 'ALL_STUDIO_UPLOADS', 'coverage': 'INCONCLUSIVE' if status['restore_guard'] else 'NOT_RESTORED',
                    'release_eligible': False, 'reason': 'NO_COMPLETE_EXTERNAL_COVERAGE_VERIFIER', 'targets': targets,
                    'unknown_external_works': 'UNBOUNDED_AFTER_BACKUP' if status['restore_guard'] else 'NOT_ASSESSED'}


class ReadOnlyOperations(OperationsMethods):
    def __init__(self, db):self.db = db

    @contextmanager
    def transaction(self):yield self.db
