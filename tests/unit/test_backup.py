from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from loopcast.backup import create_backup, verify_backup, publish_directory
from loopcast.domain import uid
from loopcast.ownership import DirectoryLock
from loopcast.storage import Store
from loopcast.feedback_models import DecisionCreate
from loopcast.social import SocialAdapter


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'state'
        self.store = Store(self.source); self.output = self.root / 'backup'

    def seed(self):
        run, _ = self.store.create({'title': 'Private fixture title'}, uid()); attempt = self.store.claim()
        data = b'fixture report'; name = uid() + '.txt'; (self.source / 'artifacts' / name).write_bytes(data)
        artifact = {'id': uid(), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'storage_ref': name}
        self.store.finish(run['run_id'], attempt['token'], 'SUCCEEDED', artifact=artifact)
        self.store.studio_authorize('douyin', None, 'qr', 'owner', uid()); job = self.store.studio_claim_job()
        self.store.studio_authorization_result(job, {'stable_subject': 'backup-owner', 'label': 'Fixture'}, uid(), 'contract')
        account = self.store.studio_accounts()['items'][0]
        data = b'unit-only registered media'; identity = uid(); name = identity + '.mp4'
        (self.source / 'library').mkdir(); (self.source / 'library' / name).write_bytes(data)
        asset = self.store.studio_register_asset({'id': identity, 'name': 'Fixture', 'kind': 'video', 'media_type': 'video/mp4',
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'storage_ref': name, 'origin': 'import', 'source': 'unit',
            'metadata': {'duration': 1, 'width': 64, 'height': 64, 'codec': 'vp9'}, 'fingerprint': None}, uid())[0]
        caps = {p['platform']: p for p in SocialAdapter(self.source, {'backend': 'contract'}).capabilities()}
        value = {'asset_id': asset['id'], 'title': 'Fixture', 'description': '', 'cover_id': None,
                 'targets': [{'account_id': account['id'], 'fields': {'category': None, 'visibility': 'public'}}]}
        preflight = self.store.studio_preflight(value, caps, 'contract')
        batch = self.store.studio_create_release(value, preflight['digest'], uid(), caps, 'contract'); target = batch['targets'][0]
        self.store.studio_dispatch(batch['id'], target['id'], 1, uid(), caps, True); job = self.store.studio_claim_job()
        self.store.studio_sending(job); self.store.studio_release_result(job, {'outcome': 'UNKNOWN'}, 'contract')
        self.store.feedback_bind({'account_id': account['id'], 'asset_id': asset['id'], 'work_id': 'manual-work', 'release_target_id': target['id'],
            'published_at': (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(), 'source': 'unit fixture'}, uid())
        raw = json.dumps({'format': 'loopcast.metrics.v1', 'source': 'Raw fixture export', 'evidence_mode': 'contract', 'rows': [{
            'platform': 'douyin', 'stable_subject': 'backup-owner', 'work_id': 'manual-work', 'asset_id': asset['id'],
            'observed_at': datetime.now(timezone.utc).isoformat(), 'aggregation': 'cumulative', 'definition': 'platform_reported_v1', 'metrics': {'views': 300}}]}).encode()
        report = self.store.feedback_import(raw, 'original.json', uid())
        proposal = self.store.feedback_propose(DecisionCreate(snapshot_ids=[report['rows'][0]['snapshot_id']], topic='Next round').model_dump(), uid())
        self.store.feedback_confirm(proposal['id'], {'expected_version': 1, 'topic': 'Reviewed', 'brief': '', 'project_id': None, 'expected_project_version': None}, uid())
        secrets = self.source / 'social' / 'credentials'; secrets.mkdir(exist_ok=True)
        (secrets / 'private.json').write_text('private-backup-canary')

    def test_wal_all_facts_files_feedback_and_credentials_exclusion(self):
        self.seed()
        # Keep an actual committed WAL open; the primary file alone lacks this row.
        db = sqlite3.connect(self.store.database); self.addCleanup(db.close)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO studio_projects VALUES('wal-project','{}',1,'created','updated')"); db.commit()
        self.assertTrue(Path(str(self.store.database) + '-wal').exists())
        before = {row[0]: db.execute('SELECT * FROM "' + row[0] + '"').fetchall() for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = create_backup(self.source, self.output); manifest = verify_backup(self.output, result['manifest_sha256'])
        with closing(sqlite3.connect(self.output / 'state.sqlite3')) as copied:
            for table, records in before.items():self.assertEqual(copied.execute('SELECT * FROM "' + table + '"').fetchall(), records, table)
        self.assertEqual(manifest['credentials_policy'], 'excluded_reauthorize')
        self.assertFalse((self.output / 'social').exists()); self.assertFalse((self.output / 'service.lock').exists())
        self.assertTrue(any(f['path'].startswith('library/') for f in manifest['files']))
        self.assertTrue(any(f['path'].startswith('artifacts/') for f in manifest['files']))
        self.assertNotIn('private-backup-canary', json.dumps(result)); self.assertNotIn(str(self.source), json.dumps(result))
        for path in self.output.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_active_owner_rejects_and_releases_lock_after_failure(self):
        with DirectoryLock(self.source):
            with self.assertRaises(BlockingIOError):create_backup(self.source, self.output)
        with patch('loopcast.backup.code_identity', side_effect=OSError('controlled disk failure')):
            with self.assertRaises(OSError):create_backup(self.source, self.output)
        self.assertFalse(self.output.exists()); self.assertEqual(list(self.root.glob('.loopcast-backup-*')), [])
        create_backup(self.source, self.output)

    def test_missing_corrupt_and_changed_files_never_publish(self):
        self.seed(); media = next((self.source / 'library').iterdir())
        original = media.read_bytes(); media.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):create_backup(self.source, self.output)
        self.assertFalse(self.output.exists()); media.unlink()
        with self.assertRaises(FileNotFoundError):create_backup(self.source, self.output)
        media.write_bytes(original)
        with patch('loopcast.backup.shutil.copyfileobj', side_effect=OSError(28, 'controlled ENOSPC')):
            with self.assertRaises(OSError):create_backup(self.source, self.output)
        self.assertFalse(self.output.exists()); self.assertEqual(list(self.root.glob('.loopcast-backup-*')), [])

    def test_paths_symlinks_and_concurrent_destination_are_rejected(self):
        alias = self.root / 'alias'; alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):create_backup(alias, self.output)
        with self.assertRaises(ValueError):create_backup(self.source, self.source / 'nested-backup')
        outside = self.root / 'outside'; outside.write_text('do not touch')
        (self.source / 'service.lock').unlink()
        (self.source / 'service.lock').symlink_to(outside)
        with self.assertRaises(OSError):create_backup(self.source, self.output)
        (self.source / 'service.lock').unlink(); self.assertEqual(outside.read_text(), 'do not touch')
        staging = self.root / 'staging'; staging.mkdir(); (staging / 'keep').write_text('data'); self.output.mkdir()
        with self.assertRaises(FileExistsError):publish_directory(staging, self.output)
        self.assertEqual(list(self.output.iterdir()), []); self.assertTrue((staging / 'keep').exists())

    def test_verify_pinned_manifest_integrity_reference_set_and_unknown_major(self):
        self.seed(); result = create_backup(self.source, self.output)
        with self.assertRaises(ValueError):verify_backup(self.output, '0' * 64)
        original = (self.output / 'manifest.json').read_bytes()
        manifest = json.loads(original); manifest['format'] = 'loopcast.backup.v99'
        raw = json.dumps(manifest).encode(); (self.output / 'manifest.json').write_bytes(raw)
        with self.assertRaises(ValueError):verify_backup(self.output, hashlib.sha256(raw).hexdigest())
        (self.output / 'manifest.json').write_bytes(original)
        media = next((self.output / 'library').iterdir()); media.write_bytes(b'changed')
        with self.assertRaises(ValueError):verify_backup(self.output, result['manifest_sha256'])
        media.unlink(); media.symlink_to(self.root / 'not-a-file')
        with self.assertRaises(ValueError):verify_backup(self.output, result['manifest_sha256'])

    def test_source_unknown_schema_and_escaping_registered_reference_rejected(self):
        self.seed()
        with self.store.transaction(True) as db:db.execute("UPDATE studio_assets SET storage_ref='../outside'")
        with self.assertRaises(ValueError):create_backup(self.source, self.output)
        with self.store.transaction(True) as db:db.execute('UPDATE schema_version SET version=999')
        with self.assertRaises(ValueError):create_backup(self.source, self.output)
        self.assertFalse(self.output.exists())
