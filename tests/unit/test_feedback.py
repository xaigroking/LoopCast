from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from loopcast.domain import Problem, uid
from loopcast.storage import Store
from loopcast.feedback_models import DecisionCreate
from loopcast.social import SocialAdapter


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.store=Store(self.temp.name)
        session=self.store.studio_authorize('douyin',None,'qr','owner',uid());job=self.store.studio_claim_job()
        self.store.studio_authorization_result(job,{'stable_subject':'stable-work-owner','label':'Unit account'},uid(),'contract')
        self.account=self.store.studio_accounts()['items'][0]
        identity=uid();value={'id':identity,'name':'Registered fixture','kind':'video','media_type':'video/mp4','size':10,'sha256':'a'*64,'storage_ref':identity+'.mp4','origin':'import','source':'unit fixture','metadata':{'duration':1.0,'width':64,'height':64,'codec':'vp9'},'fingerprint':None}
        self.asset=self.store.studio_register_asset(value,uid())[0]
        self.published=(datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
        self.observed=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        self.binding_value={'account_id':self.account['id'],'asset_id':self.asset['id'],'work_id':'work-one','release_target_id':None,'published_at':self.published,'source':'Manual unit fixture'}

    def bind(self):return self.store.feedback_bind(self.binding_value,uid())
    def document(self,**row):
        return {'format':'loopcast.metrics.v1','source':'Manual export fixture','evidence_mode':'contract','rows':[{'platform':'douyin','stable_subject':'stable-work-owner','work_id':'work-one','asset_id':self.asset['id'],'observed_at':self.observed,'aggregation':'cumulative','definition':'platform_reported_v1','metrics':{'views':200,'likes':None},**row}]}
    def ingest(self,document=None,key=None):return self.store.feedback_import(json.dumps(document or self.document()).encode(),'observations.json',key or uid())

    def test_raw_file_identity_missing_values_timezone_and_restart(self):
        self.bind();raw=json.dumps(self.document(),indent=3).encode();report=self.store.feedback_import(raw,'original.json',uid())
        self.assertEqual(report['rows'][0]['state'],'ACCEPTED');self.assertEqual(report['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.feedback_raw(report['id'])['data'],raw)
        self.assertEqual(self.store.feedback_import(raw,'renamed.json',uid())['id'],report['id'])
        snapshot=self.store.feedback_workspace()['snapshots'][0]
        self.assertIsNone(snapshot['metrics']['likes']);self.assertEqual(snapshot['original_observed_at'],self.observed)
        self.assertEqual(Store(self.temp.name).feedback_workspace(),self.store.feedback_workspace())
        shifted=datetime.fromisoformat(self.observed).astimezone(timezone(timedelta(hours=8))).isoformat()
        duplicate=self.ingest(self.document(observed_at=shifted));self.assertEqual(duplicate['rows'][0]['state'],'DUPLICATE')
        self.assertEqual(len(self.store.feedback_workspace()['snapshots']),1)

    def test_quarantine_reprocess_is_versioned_and_never_guesses_from_title(self):
        report=self.ingest();self.assertEqual(report['rows'][0]['code'],'WORK_UNBOUND')
        self.assertEqual(self.store.feedback_workspace()['snapshots'],[])
        self.bind();updated=self.store.feedback_reprocess(report['id'],1,'reprocess')
        self.assertEqual(updated['rows'][0]['state'],'ACCEPTED');self.assertEqual(updated['version'],2)
        self.assertEqual(len(updated['resolution_history']),2)
        self.assertEqual(self.store.feedback_reprocess(report['id'],1,'reprocess'),updated)
        with self.assertRaises(Problem):self.store.feedback_reprocess(report['id'],1,'different-key')

    def test_concurrent_imports_do_not_accumulate_and_conflicts_preserve_original(self):
        self.bind()
        with ThreadPoolExecutor(8) as pool:reports=list(pool.map(lambda _:self.ingest(),range(12)))
        self.assertEqual(len({r['id'] for r in reports}),1);self.assertEqual(len(self.store.feedback_workspace()['snapshots']),1)
        conflict=self.ingest(self.document(metrics={'views':201}))
        self.assertEqual(conflict['rows'][0]['code'],'OBSERVATION_CONFLICT')
        self.assertEqual(self.store.feedback_workspace()['snapshots'][0]['metrics']['views'],200)
        later=(datetime.fromisoformat(self.observed)+timedelta(seconds=1)).isoformat()
        good=self.ingest(self.document(observed_at=later,metrics={'views':220}));self.assertEqual(good['rows'][0]['state'],'ACCEPTED')
        bad=self.ingest(self.document(observed_at=(datetime.fromisoformat(later)+timedelta(seconds=1)).isoformat(),metrics={'views':10}))
        self.assertEqual(bad['rows'][0]['code'],'COUNTER_DECREASE');self.assertEqual(len(self.store.feedback_workspace()['snapshots']),2)

    def test_bad_timestamps_mapping_and_deltas_are_isolated(self):
        self.bind()
        for row,code in [({'observed_at':'2026-01-01T00:00:00'},'OBSERVATION_TIME_INVALID'),({'observed_at':'0001-01-01T00:00:00+01:00'},'OBSERVATION_TIME_INVALID'),({'asset_id':'b'*32},'MEDIA_MAPPING_MISMATCH'),({'metrics':{}},'METRICS_MISSING'),({'metrics':{'views':1.5}},'COUNTER_TYPE_INVALID')]:
            self.assertEqual(self.ingest(self.document(**row))['rows'][0]['code'],code)
        start=(datetime.fromisoformat(self.observed)-timedelta(hours=2)).isoformat();end=(datetime.fromisoformat(self.observed)-timedelta(hours=1)).isoformat()
        value=self.document(aggregation='delta',window_start=start,window_end=end)
        self.assertEqual(self.ingest(value)['rows'][0]['state'],'ACCEPTED')
        overlap=self.document(aggregation='delta',window_start=start,window_end=self.observed)
        self.assertEqual(self.ingest(overlap)['rows'][0]['code'],'DELTA_OVERLAP')
        self.assertEqual(len(self.store.feedback_workspace()['snapshots']),1)

    def test_invalid_document_and_unexpected_failure_leave_no_partial_import(self):
        self.bind()
        document=self.document();document['rows'].append(document['rows'][0]|{'metrics':{'views':True}})
        with self.assertRaises(ValidationError):self.ingest(document)
        self.assertEqual(self.store.feedback_workspace()['imports'],[])
        original=self.store._feedback_ingest_row
        document=self.document();document['rows'].append(document['rows'][0]|{'metrics':{'views':250}})
        calls=[]
        def fail_second(*args):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('Controlled persistence failure')
            return original(*args)
        with patch.object(self.store,'_feedback_ingest_row',side_effect=fail_second):
            with self.assertRaises(RuntimeError):self.ingest(document,key='retry-after-rollback')
        state=self.store.feedback_workspace()
        self.assertEqual(state['imports'],[]);self.assertEqual(state['snapshots'],[])
        report=self.ingest(document,key='retry-after-rollback')
        self.assertEqual([row['state'] for row in report['rows']],['ACCEPTED','QUARANTINED'])
        self.assertEqual(len(self.store.feedback_workspace()['snapshots']),1)

    def test_confirmed_receipt_rejects_different_work_identity(self):
        caps={p['platform']:p for p in SocialAdapter(self.temp.name,{'backend':'contract'}).capabilities()}
        value={'asset_id':self.asset['id'],'title':'Published fixture','cover_id':None,'description':'','targets':[{'account_id':self.account['id'],'fields':{'category':None,'visibility':'public','declaration':None}}]}
        check=self.store.studio_preflight(value,caps,'contract');batch=self.store.studio_create_release(value,check['digest'],uid(),caps,'contract');target=batch['targets'][0]
        self.store.studio_dispatch(batch['id'],target['id'],1,uid(),caps,True);job=self.store.studio_claim_job();self.store.studio_sending(job)
        self.assertTrue(self.store.studio_release_result(job,{'outcome':'PUBLISHED','confirmed':True,'work_id':'confirmed-work'},'contract'))
        self.assertEqual(self.store.studio_release(batch['id'])['targets'][0]['state'],'PUBLISHED')
        with self.assertRaises(Problem) as error:self.store.feedback_bind(self.binding_value|{'release_target_id':target['id']},uid())
        self.assertEqual(error.exception.code,'WORK_IDENTITY_MISMATCH')
        binding=self.store.feedback_bind(self.binding_value|{'release_target_id':target['id'],'work_id':'confirmed-work'},uid())
        self.assertEqual(binding['evidence_kind'],'PLATFORM_RECEIPT');self.assertEqual(binding['evidence_mode'],'contract')

    def test_evidence_modes_and_work_mapping_cannot_be_overridden(self):
        self.bind();document=self.document();document['evidence_mode']='live'
        self.assertEqual(self.ingest(document)['rows'][0]['code'],'WORK_UNBOUND')
        with self.assertRaises(Problem):self.store.feedback_bind(self.binding_value|{'published_at':self.observed},uid())
        self.assertEqual(self.store.feedback_workspace()['snapshots'],[])

    def test_manual_metrics_do_not_clear_unknown_send_or_confirm_publication(self):
        caps={p['platform']:p for p in SocialAdapter(self.temp.name,{'backend':'contract'}).capabilities()}
        value={'asset_id':self.asset['id'],'title':'Unknown fixture','cover_id':None,'description':'','targets':[{'account_id':self.account['id'],'fields':{'category':None,'visibility':'public','declaration':None}}]}
        check=self.store.studio_preflight(value,caps,'contract');batch=self.store.studio_create_release(value,check['digest'],uid(),caps,'contract');target=batch['targets'][0]
        self.store.studio_dispatch(batch['id'],target['id'],1,uid(),caps,True);job=self.store.studio_claim_job();self.store.studio_sending(job)
        self.store.studio_release_result(job,{'outcome':'UNKNOWN'},'contract')
        bound=self.store.feedback_bind(self.binding_value|{'release_target_id':target['id']},uid());self.assertEqual(bound['evidence_kind'],'MANUAL_ASSOCIATION')
        self.assertEqual(self.ingest()['rows'][0]['state'],'ACCEPTED')
        current=self.store.studio_release(batch['id'])['targets'][0];self.assertEqual(current['state'],'UNKNOWN');self.assertFalse(current['can_dispatch'])
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM release_account_locks').fetchone()[0],1)

    def test_decision_freezes_inputs_thresholds_and_user_revisions_without_generating(self):
        self.bind();report=self.ingest();snapshot=report['rows'][0]['snapshot_id']
        value=DecisionCreate(snapshot_ids=[snapshot],topic='Next topic',brief='User review').model_dump()
        decision=self.store.feedback_propose(value,'propose');self.assertEqual(decision['reason'],'TOP_OBSERVED_VIEWS')
        self.assertEqual(decision['selected_snapshot_id'],snapshot);self.assertEqual(decision['strategy_version'],'manual-observation-v1')
        self.assertEqual(decision['snapshot_digests'][snapshot],self.store.feedback_workspace()['snapshots'][0]['sha256'])
        confirmed=self.store.feedback_confirm(decision['id'],{'expected_version':1,'topic':'Edited topic','brief':'Edited brief','project_id':None,'expected_project_version':None},'confirm')
        link=confirmed['revisions'][0];project=self.store.studio_project(link['project_id'])
        self.assertEqual(project['revisions'][0]['decision_id'],decision['id']);self.assertEqual(project['draft']['title'],'Edited topic')
        self.assertEqual(confirmed['input']['topic'],'Next topic');self.assertEqual(self.store.list_runs()['items'],[])
        changed=self.store.studio_save(project['id'],project['draft']|{'script':'Later explicit script'},project['version'],uid())
        frozen=self.store.studio_revision(project['id'],changed['version'],uid());self.assertEqual(frozen['revisions'][0]['decision_id'],decision['id'])
        self.assertEqual(Store(self.temp.name).feedback_decisions()[0]['snapshot_digests'],decision['snapshot_digests'])
        fallback=self.store.feedback_propose(DecisionCreate(snapshot_ids=[],topic='Manual fallback').model_dump(),uid());self.assertEqual(fallback['reason'],'INSUFFICIENT_DATA')
        immature=self.store.feedback_propose(value|{'minimum_age_hours':100},uid());self.assertEqual(immature['excluded'][snapshot],'IMMATURE')
        missing=self.store.feedback_propose(value|{'minimum_views':1000},uid());self.assertEqual(missing['excluded'][snapshot],'LOW_SAMPLE')
