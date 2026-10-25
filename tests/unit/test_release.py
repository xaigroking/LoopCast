from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from loopcast.storage import Store
from loopcast.domain import Problem, uid
from loopcast.social import SocialAdapter


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.adapter = SocialAdapter(self.temp.name, {'backend': 'contract', 'sending_enabled': True})
        self.caps = {p['platform']: p for p in self.adapter.capabilities()}

    def account(self, subject='stable', platform='douyin', owner='owner'):
        session = self.store.studio_authorize(platform, None, 'qr', owner, uid())
        job = self.store.studio_claim_job()
        self.assertEqual(job['subject_id'], session['id'])
        self.assertTrue(self.store.studio_authorization_result(job, {'stable_subject': subject, 'label': subject}, uid(), 'contract'))
        return next(a for a in self.store.studio_accounts()['items'] if a['stable_subject'] == subject)

    def asset(self, sha='a' * 64, parent=None, fingerprint=None):
        identity = uid()
        value = {'id': identity, 'name': 'media', 'kind': 'video', 'media_type': 'video/mp4', 'size': 20, 'sha256': sha,
                 'storage_ref': identity + '.mp4', 'origin': 'import', 'source': 'unit fixture', 'metadata': {'duration': 1.0, 'width': 64, 'height': 64, 'codec': 'h264'}, 'fingerprint': fingerprint}
        return self.store.studio_register_asset(value, uid(), parent)[0]

    def batch(self, account, asset, key=None):
        value = {'asset_id': asset['id'], 'title': 'A title', 'description': '', 'cover_id': None,
                 'targets': [{'account_id': account['id'], 'fields': {'category': None, 'visibility': 'public'}}]}
        check = self.store.studio_preflight(value, self.caps, 'contract')
        self.assertTrue(check['allowed'], check)
        return self.store.studio_create_release(value, check['digest'], key or uid(), self.caps, 'contract'), value

    def dispatch(self, batch):
        target = batch['targets'][0]
        self.store.studio_dispatch(batch['id'], target['id'], target['version'], uid(), self.caps, True)
        job = self.store.studio_claim_job()
        self.assertTrue(self.store.studio_sending(job))
        return job

    def test_authorization_owner_cancellation_expiry_and_stable_relogin(self):
        session = self.store.studio_authorize('douyin', None, 'qr', 'owner-a', 'first')
        with self.assertRaises(Problem): self.store.studio_authorization(session['id'], 'owner-b')
        self.assertEqual(self.store.studio_cancel_authorization(session['id'], 'owner-a', 'cancel')['state'], 'CANCELLED')
        self.assertIsNone(self.store.studio_claim_job())
        first = self.account('same', owner='owner-a')
        again = self.account('same', owner='owner-b')
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len(self.store.studio_accounts()['items']), 1)
        expiring = self.store.studio_authorize('youtube', None, 'browser', 'owner', uid())
        with patch('loopcast.account_storage.time.time', return_value=time.time() + 1000):
            self.assertEqual(self.store.studio_authorization(expiring['id'], 'owner')['state'], 'EXPIRED')

    def test_permanent_guard_survives_copies_lineage_renames_restart_and_time(self):
        account, asset = self.account(), self.asset()
        batch, value = self.batch(account, asset)
        job = self.dispatch(batch)
        self.assertTrue(self.store.studio_release_result(job, {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': 'confirmed-123'}, 'contract'))
        copy = self.asset()
        derivative = self.asset('b' * 64, parent=copy['id'])
        self.store.studio_account_update(account['id'], 'New nickname', [], account['version'], uid())
        self.store = Store(self.temp.name)
        for candidate in (asset, copy, derivative):
            check = self.store.studio_preflight(value | {'asset_id': candidate['id'], 'title': 'Renamed'}, self.caps, 'contract')
            self.assertIn('PERMANENT_DUPLICATE', check['targets'][0]['reasons'])
        with patch('loopcast.release_storage.time.time', return_value=time.time() + 10 * 365 * 86400):
            self.assertFalse(self.store.studio_preflight(value, self.caps, 'contract')['allowed'])

    def test_concurrent_dispatch_has_one_attempt_and_unknown_never_resends(self):
        account, asset = self.account(), self.asset()
        batch, _ = self.batch(account, asset); target = batch['targets'][0]
        def dispatch(key):
            try: return self.store.studio_dispatch(batch['id'], target['id'], 1, key, self.caps, True)
            except Problem: return None
        with ThreadPoolExecutor(8) as pool: results = list(pool.map(dispatch, [uid() for _ in range(8)]))
        self.assertEqual(sum(r is not None for r in results), 1)
        job = self.store.studio_claim_job(); self.assertTrue(self.store.studio_sending(job))
        self.store.studio_release_result(job, {'outcome': 'PUBLISHED', 'work_id': 'not-confirmed'}, 'contract')
        current = self.store.studio_release(batch['id'])
        self.assertEqual(current['targets'][0]['state'], 'UNKNOWN')
        with self.assertRaises(Problem): self.store.studio_dispatch(batch['id'], target['id'], current['targets'][0]['version'], uid(), self.caps, True)
        self.assertEqual(len(current['targets'][0]['attempts']), 1)

    def test_recovery_keeps_ambiguous_account_lock_and_rejects_late_results(self):
        account, asset = self.account(), self.asset()
        batch, _ = self.batch(account, asset); job = self.dispatch(batch)
        self.store.studio_recover()
        self.assertFalse(self.store.studio_release_result(job, {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': 'late'}, 'contract'))
        self.assertEqual(self.store.studio_release(batch['id'])['targets'][0]['state'], 'UNKNOWN')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM release_account_locks').fetchone()[0], 1)

    def test_cancel_before_boundary_allows_new_frozen_input_without_clearing_history(self):
        account, asset = self.account(), self.asset()
        batch, _ = self.batch(account, asset)
        target = batch['targets'][0]
        cancelled = self.store.studio_cancel_release(batch['id'], target['id'], 1, uid(), [])
        self.assertEqual(cancelled['targets'][0]['state'], 'CANCELLED')
        next_batch, _ = self.batch(account, asset)
        self.assertNotEqual(next_batch['id'], batch['id'])
        self.assertEqual(len(self.store.studio_releases()['items']), 2)

    def test_contract_receipts_never_become_live_account_facts(self):
        account, asset = self.account(), self.asset()
        _, value = self.batch(account, asset)
        check = self.store.studio_preflight(value, self.caps, 'live')
        self.assertIn('EVIDENCE_MISMATCH', check['targets'][0]['reasons'])

    def test_unconfirmed_submission_is_not_publication_and_query_is_capability_gated(self):
        account, asset = self.account(), self.asset()
        batch, _ = self.batch(account, asset); job = self.dispatch(batch)
        self.store.studio_release_result(job, {'outcome': 'SUBMITTED'}, 'sau')
        current = self.store.studio_release(batch['id'], [])
        self.assertEqual(current['state'], 'UNRESOLVED')
        self.assertFalse(current['targets'][0]['can_verify'])
        with self.assertRaises(Problem): self.store.studio_verify(batch['id'], current['targets'][0]['id'], current['targets'][0]['version'], uid(), [])

    def test_queued_deadlines_retire_unsent_locks_and_pending_checks(self):
        account, asset = self.account(), self.asset()
        batch, _ = self.batch(account, asset); target = batch['targets'][0]
        self.store.studio_dispatch(batch['id'],target['id'],1,uid(),self.caps,True)
        with patch('loopcast.account_storage.time.time',return_value=time.time()+1000):
            self.assertIsNone(self.store.studio_claim_job())
        current = self.store.studio_release(batch['id'])
        self.assertEqual(current['targets'][0]['state'],'NOT_SENT')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM release_account_locks').fetchone()[0],0)
        self.store.studio_check_account(account['id'],account['version'],uid())
        with patch('loopcast.account_storage.time.time',return_value=time.time()+1000):
            self.assertIsNone(self.store.studio_claim_job())
        self.assertEqual(self.store.studio_accounts()['items'][0]['state'],'EXPIRED')
