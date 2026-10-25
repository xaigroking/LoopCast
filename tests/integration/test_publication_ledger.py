from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from loopcast.storage import Store,Problem


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)

    def complete(self,key):
        run,_=self.store.create({'title':key},key);job=self.store.claim()
        self.store.finish(run['run_id'],job['token'],'SUCCEEDED',artifact={'id':key,'size':1,'sha256':'a'*64,'storage_ref':key+'.txt'})
        return self.store.snapshot(run['run_id'])

    def intent(self,run,account='demo'):
        return self.store.publication_create(run['run_id'],{'platform':'simulator','account':account},'intent-'+account,run['state_version'])

    def dispatch(self,run,key='dispatch'):
        return self.store.publication_dispatch(run['run_id'],run['publications'][-1]['id'],key,run['state_version'])

    def test_concurrent_permanent_identity_and_account_lock(self):
        run=self.complete('first')
        def create(i):
            try:return self.store.publication_create(run['run_id'],{'platform':'simulator','account':'demo'},str(i),run['state_version'])
            except Problem as e:return e.code
        with ThreadPoolExecutor(16) as pool:results=list(pool.map(create,range(100)))
        run=self.store.snapshot(run['run_id']);self.assertEqual(len(run['publications']),1)
        intent_id=run['publications'][0]['id']
        def dispatch(index):
            try:return self.dispatch(run,'concurrent-'+str(index))
            except Problem as error:return error.code
        with ThreadPoolExecutor(16) as pool: dispatched=list(pool.map(dispatch,range(100)))
        accepted=[x for x in dispatched if isinstance(x,tuple)]
        self.assertEqual(len(accepted),1)
        snapshot,job=accepted[0]
        other=self.intent(self.complete('second'))
        with self.assertRaises(Problem) as err:self.dispatch(other)
        self.assertEqual(err.exception.code,'ACCOUNT_BUSY')
        self.assertTrue(self.store.publication_sending(job))
        self.assertTrue(self.store.publication_result(job,'PUBLISHED','sim-'+'a'*32,evidence='Simulator only'))
        self.assertFalse(self.store.publication_result(job,'UNKNOWN'))
        run=self.store.snapshot(run['run_id'])
        with self.store.transaction(True) as db:
            db.execute("UPDATE runs SET input=? WHERE id=?",(json.dumps({'title':'renamed','cookie':'changed','transcoded_sha256':'new'}),run['run_id']))
        for index in range(3):
            run=Store(self.temp.name).snapshot(run['run_id'])
            run=self.store.publication_create(run['run_id'],{'platform':'simulator','account':'demo'},'new-'+str(index),run['state_version'])
            self.assertEqual(run['publications'][0]['id'],intent_id)
            self.assertEqual(run['publications'][0]['state'],'PUBLISHED')
            with self.assertRaises(Problem):self.dispatch(run,'again-'+str(index))
        self.assertIsNotNone(self.dispatch(other)[1])

    def test_ambiguous_send_recovery_and_fencing(self):
        run=self.intent(self.complete('first'));prepared,job=self.dispatch(run)
        self.assertTrue(self.store.publication_sending(job))
        self.store.publication_recover()
        unknown=self.store.snapshot(run['run_id']);self.assertEqual(unknown['publications'][0]['state'],'UNKNOWN')
        self.assertFalse(self.store.publication_result(job,'PUBLISHED','sim-'+'a'*32))
        with self.assertRaises(Problem):self.dispatch(unknown,'new')
        verified=self.store.publication_verify(run['run_id'],job['intent_id'],'verify',unknown['state_version'])
        self.assertEqual(verified['publications'][0]['state'],'UNKNOWN')
        self.assertEqual(len(verified['publications'][0]['receipts']),1)
        self.assertEqual(self.store.publication_verify(run['run_id'],job['intent_id'],'verify',unknown['state_version']),verified)
        with self.assertRaises(Problem):self.store.publication_verify(run['run_id'],job['intent_id'],'stale',unknown['state_version'])

    def test_preboundary_recovery_and_expired_lease_never_reclaims_sender(self):
        run=self.intent(self.complete('first'));prepared,job=self.dispatch(run)
        self.store.publication_recover();run=self.store.snapshot(run['run_id'])
        self.assertEqual(run['publications'][0]['state'],'NOT_SENT')
        run,job=self.dispatch(run,'retry');self.assertTrue(self.store.publication_sending(job))
        with self.store.transaction(True) as db:db.execute('UPDATE publish_attempts SET lease_until=? WHERE token=?',(time.time()-1,job['token']))
        self.assertFalse(self.store.publication_result(job,'PUBLISHED','sim-'+'b'*32))
        self.store.publication_recover();run=self.store.snapshot(run['run_id'])
        self.assertEqual(run['publications'][0]['state'],'UNKNOWN')
        other=self.intent(self.complete('other'))
        with self.assertRaises(Problem):self.dispatch(other)

    def test_targets_are_independent_and_missing_identity_stays_unknown(self):
        run=self.intent(self.complete('first'),'left');run,job=self.dispatch(run)
        self.store.publication_sending(job);self.store.publication_result(job,'PUBLISHED','sim-'+'a'*32)
        run=self.store.snapshot(run['run_id']);run=self.intent(run,'right');run,job=self.dispatch(run,'right')
        self.store.publication_sending(job);self.store.publication_result(job,'PUBLISHED',None,evidence='submitted exit 0')
        run=self.store.snapshot(run['run_id'])
        self.assertEqual([p['state'] for p in run['publications']],['PUBLISHED','UNKNOWN'])
        self.assertFalse(run['allowed_actions']['retry']['enabled'])
        receipts=Path(self.temp.name)/'simulator';receipts.mkdir()
        (receipts/(job['attempt_id']+'.json')).write_text(json.dumps({'attempt_id':job['attempt_id'],'work_id':'sim-'+'c'*32}))
        final=self.store.publication_verify(run['run_id'],job['intent_id'],'query',run['state_version'])
        self.assertEqual([p['state'] for p in final['publications']],['PUBLISHED','PUBLISHED'])

    def test_expired_prepared_queue_item_can_record_definite_no_send(self):
        run=self.intent(self.complete('queued'));run,job=self.dispatch(run)
        with self.store.transaction(True) as db:db.execute('UPDATE publish_attempts SET lease_until=0 WHERE token=?',(job['token'],))
        self.assertFalse(self.store.publication_sending(job))
        self.assertTrue(self.store.publication_abandon_prepared(job))
        self.assertFalse(self.store.publication_sending(job))
        current=self.store.snapshot(run['run_id'])
        self.assertEqual(current['publications'][0]['state'],'NOT_SENT')
        self.assertIsNotNone(self.dispatch(current,'replacement')[1])
