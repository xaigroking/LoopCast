from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from loopcast import migrations
from loopcast.backup import create_backup
from loopcast.domain import Problem, uid
from loopcast.media import MediaLibrary
from loopcast.restore import restore_backup, reconciliation_report
from loopcast.social import SocialAdapter
from loopcast.storage import Store
from loopcast.studio_service import StudioControl


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root / 'source')
        self.social = SocialAdapter(self.store.directory, {'backend': 'contract', 'sending_enabled': True})
        self.caps = {c['platform']: c for c in self.social.capabilities()}
        self.store.studio_authorize('douyin', None, 'qr', 'owner', uid()); job = self.store.studio_claim_job()
        self.reference = uid(); (self.social.credentials / (self.reference + '.json')).write_text('{}')
        self.store.studio_authorization_result(job, {'stable_subject': 'restore-owner', 'label': 'Fixture'}, self.reference, 'contract')
        self.account = self.store.studio_accounts()['items'][0]
        self.library = MediaLibrary(self.store)

    def release(self):
        identity = uid(); raw = identity.encode(); path = self.library.root / (identity + '.mp4'); path.write_bytes(raw)
        asset = self.store.studio_register_asset({'id': identity, 'name': 'Unit fixture', 'kind': 'video', 'media_type': 'video/mp4',
            'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'storage_ref': path.name, 'origin': 'import', 'source': 'Unit fixture',
            'metadata': {'duration': 1, 'width': 64, 'height': 64, 'codec': 'vp9'}, 'fingerprint': None}, uid())[0]
        value = {'asset_id': asset['id'], 'title': 'Fixture', 'description': '', 'cover_id': None,
                 'targets': [{'account_id': self.account['id'], 'fields': {'category': None, 'visibility': 'public'}}]}
        check = self.store.studio_preflight(value, self.caps, 'contract')
        return self.store.studio_create_release(value, check['digest'], uid(), self.caps, 'contract')

    def queue(self, batch):
        target = batch['targets'][0]
        self.store.studio_dispatch(batch['id'], target['id'], target['version'], uid(), self.caps, True)

    def test_backup_then_external_result_then_restore_never_reuses_old_send(self):
        for outcome in ('PUBLISHED', 'UNKNOWN'):
            with self.subTest(outcome=outcome):
                batch = self.release(); self.queue(batch)
                package = self.root / ('backup-' + outcome); report = create_backup(self.store.directory, package)
                old_job = self.store.studio_claim_job(); self.assertTrue(self.store.studio_sending(old_job))
                external_uploads = [old_job['id']]
                self.assertTrue(self.store.studio_release_result(old_job, {'outcome': outcome, 'confirmed': True, 'work_id': 'external-work'}, 'contract'))
                restored_path = self.root / ('restored-' + outcome)
                result = restore_backup(package, report['manifest_sha256'], restored_path); self.assertFalse(result['upload_allowed'])
                store = Store(restored_path); status = store.operations_status()
                self.assertEqual(status['restore_guard']['state'], 'ACTIVE')
                self.assertIsNone(store.studio_claim_job()); self.assertFalse(store.studio_sending(old_job))
                self.assertFalse(store.studio_release_result(old_job, {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': 'late'}, 'contract'))
                restored = store.studio_release(batch['id']); target = restored['targets'][0]
                self.assertEqual(target['state'], 'UNKNOWN'); self.assertFalse(target['can_dispatch'])
                with store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM release_account_locks').fetchone()[0], 1)
                store.studio_authorize('douyin', self.account['id'], 'qr', 'new-owner', uid()); job = store.studio_claim_job()
                self.assertTrue(store.studio_authorization_result(job, {'stable_subject': 'restore-owner'}, uid(), 'contract'))
                with patch('loopcast.release_storage.time.time', return_value=time.time() + 10 * 365 * 86400):
                    with self.assertRaises(Problem) as error:store.studio_dispatch(batch['id'], target['id'], target['version'], uid(), self.caps, True)
                self.assertEqual(error.exception.code, 'RESTORE_GUARD_ACTIVE')
                store.studio_recover(); self.assertEqual(Store(restored_path).operations_status()['restore_guard'], status['restore_guard'])
                first = reconciliation_report(restored_path); self.assertEqual(reconciliation_report(restored_path), first)
                self.assertFalse(first['release_eligible']); self.assertEqual(first['targets'][0]['query'], 'unavailable')
                self.assertEqual(external_uploads, [old_job['id']])
                # Permit another source fixture without erasing the unknown fact.
                if outcome == 'UNKNOWN':break

    def test_guard_inserted_at_final_call_boundary_prevents_adapter_invocation(self):
        batch = self.release(); self.queue(batch); job = self.store.studio_claim_job()
        control = StudioControl(self.store, self.library, self.social)
        original = self.store.studio_sending
        manifest = {'backup_id': uid(), 'created_at': '2026-01-01T00:00:00+00:00', 'schema_version': 7}
        def interrupt(value):
            self.assertTrue(original(value))
            self.store.prepare_restored_state(manifest, 'a' * 64)
            return True
        with patch.object(self.store, 'studio_sending', side_effect=interrupt), patch.object(self.social, 'invoke') as invoke:
            control.execute_job(job, threading.Event()); invoke.assert_not_called()
        self.assertEqual(self.store.studio_release(batch['id'])['targets'][0]['state'], 'UNKNOWN')
        self.assertFalse(self.store.operations_status()['upload_allowed_by_recovery'])

    def test_known_receipt_is_evidence_for_one_target_and_never_global_release(self):
        batch = self.release(); self.queue(batch); job = self.store.studio_claim_job(); self.store.studio_sending(job)
        self.store.studio_release_result(job, {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': 'known-work'}, 'contract')
        backup = self.root / 'backup'; report = create_backup(self.store.directory, backup)
        restored = self.root / 'restored'; restore_backup(backup, report['manifest_sha256'], restored)
        store = Store(restored); report = store.operations_reconciliation(['douyin'])
        self.assertEqual(report['targets'][0]['known_publication'], 'FOUND'); self.assertEqual(report['targets'][0]['query'], 'available')
        self.assertFalse(report['release_eligible']); self.assertEqual(report['coverage'], 'INCONCLUSIVE')
        self.assertEqual(store.studio_release(batch['id'])['targets'][0]['state'], 'PUBLISHED')

    def test_invalid_package_repeated_destination_and_failed_migration_never_publish(self):
        backup = self.root / 'backup'; report = create_backup(self.store.directory, backup); target = self.root / 'restored'
        with patch.object(Store, 'prepare_restored_state', side_effect=RuntimeError('Controlled interruption')):
            with self.assertRaises(RuntimeError):restore_backup(backup, report['manifest_sha256'], target)
        self.assertFalse(target.exists()); self.assertEqual(list(self.root.glob('.loopcast-restore-*')), [])
        restore_backup(backup, report['manifest_sha256'], target)
        before = Store(target).operations_status()
        with self.assertRaises(ValueError):restore_backup(backup, report['manifest_sha256'], target)
        self.assertEqual(Store(target).operations_status(), before)
        with self.assertRaises(ValueError):restore_backup(backup, '0' * 64, self.root / 'bad')

    def test_v6_upgrade_failure_rolls_back_and_normal_upgrade_does_not_activate_guard(self):
        old = self.root / 'old'; old.mkdir(); path = old / 'state.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript(''.join(getattr(migrations, name) for name in ('VERSION_ONE','VERSION_TWO','VERSION_THREE','VERSION_FOUR','VERSION_FIVE','VERSION_SIX')))
            db.execute("INSERT INTO studio_projects VALUES('retained','{}',1,'created','updated')"); db.commit()
        backup = self.root / 'v6-backup'; package = create_backup(old, backup)
        with patch.object(migrations, 'VERSION_SEVEN', migrations.VERSION_SEVEN + '; INVALID SQL'):
            with self.assertRaises(sqlite3.OperationalError):Store(old)
            with self.assertRaises(sqlite3.OperationalError):restore_backup(backup, package['manifest_sha256'], self.root / 'failed-v6-restore')
        self.assertFalse((self.root / 'failed-v6-restore').exists())
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 6)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='restore_guard'").fetchone())
        store = Store(old); self.assertIsNone(store.operations_status()['restore_guard'])
        with store.transaction() as db:self.assertEqual(db.execute('SELECT id FROM studio_projects').fetchone()[0], 'retained')
