import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
from loopcast import migrations, social_bridge
from loopcast.domain import Problem, uid
from loopcast.inspection_models import InspectionSettings, ChannelCheckRequest, ChannelReport
from loopcast.inspection_service import InspectionService
from loopcast.media import MediaLibrary
from loopcast.runner import RunnerError
from loopcast.social import SocialAdapter
from loopcast.storage import Store
from loopcast.studio_service import StudioControl
from schedule_support import ScheduleFixture

OUTCOME={'result':'CALLABLE','reason_code':'OK','evidence_scope':'LOCAL_RUNTIME','checks':[],'actual_operations':{}}


class InspectionTests(ScheduleFixture):
    def setUp(self):
        super().setUp()
        self.store.inspection_settings=InspectionSettings(online_accounts=[self.account['id']])
        self.social=SocialAdapter(self.store.directory,{'backend':'contract','timeout':5,'sending_enabled':False,'contract_profiles':{'douyin':[{'stable_subject':'schedule-fixture','label':'Fixture'}]}})
        private=self.store.studio_private_account(self.account['id']);credential=self.social.credentials/(private['credential_ref']+'.json')
        credential.write_text(json.dumps({'stable_subject':'schedule-fixture','cookie':'credential-canary'}));credential.chmod(0o600)
        self.runtime=SimpleNamespace(store=self.store,social=self.social,generator_settings=None,media_settings=None,ready=lambda:True)
        self.service=InspectionService(self.runtime)
        self.control=StudioControl(self.store,MediaLibrary(self.store),self.social);self.control.inspections=self.service

    def channel(self, kind='CONTROL_STORAGE'):
        return next(d for d in self.service.descriptors()if d['kind']==kind)

    def begin(self,kind='CONTROL_STORAGE',online=False,key=None):
        value=self.channel(kind);version=self.service.channel(value['id'])['channel_version']
        return self.service.start(value['id'],ChannelCheckRequest(expected_version=version,online=online,confirmed_online=online),key or uid())

    def finish(self,check,outcome=None):
        job=self.store.studio_claim_job();self.assertIsNotNone(job);self.store.inspection_running(job)
        self.store.inspection_finish(job,outcome or OUTCOME)
        return self.service.report(self.store.inspection_check(check['id'])['report_id'])

    def test_strict_policy_consent_and_no_arbitrary_parameters(self):
        for value in [{'enabled':1},{'allow_live':'true'},{'concurrency':True},{'concurrency':2},{'max_pending':0},{'online_accounts':[self.account['id']]*2},{'backoff_initial_seconds':120,'backoff_max_seconds':60}]:
            with self.assertRaises(ValidationError):InspectionSettings.model_validate(value)
        for value in [{'expected_version':True},{'expected_version':1,'online':1},{'expected_version':1,'online':True},{'expected_version':1,'command':'upload'}]:
            with self.assertRaises(ValidationError):ChannelCheckRequest.model_validate(value)

    def test_get_inventory_and_capabilities_never_probe_or_mutate(self):
        before=self.store.database.read_bytes()
        with patch.object(self.social,'invoke',side_effect=AssertionError('No operation')),patch('loopcast.inspection_service.run',side_effect=AssertionError('No process')):
            result=self.service.list();self.assertEqual(len(result['items']),4)
            self.assertTrue(all(c['freshness']=='NOT_CHECKED'for c in result['items']))
            self.assertEqual(self.store.database.read_bytes(),before)
        with self.store.transaction()as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM inspection_checks').fetchone()[0],0)

    def test_eight_concurrent_requests_single_flight_and_persistent_replay(self):
        with ThreadPoolExecutor(max_workers=8)as pool:values=list(pool.map(lambda _:self.begin(),range(8)))
        self.assertEqual(len({v['check']['id']for v in values}),1)
        check=values[0]['check'];report=self.finish(check);self.assertEqual(report['result'],'CALLABLE')
        key=uid();value=self.channel();body=ChannelCheckRequest(expected_version=1)
        first=self.service.start(value['id'],body,key)
        replay=self.service.start(value['id'],body,key);self.assertEqual(first['check']['id'],replay['check']['id'])
        with self.assertRaises(Problem):self.service.start(value['id'],ChannelCheckRequest(expected_version=2),key)

    def test_account_concurrent_requests_call_only_declared_check_once(self):
        with ThreadPoolExecutor(max_workers=8)as pool:values=list(pool.map(lambda _:self.begin('ACCOUNT',True),range(8)))
        self.assertEqual(len({v['check']['id']for v in values}),1)
        job=self.store.studio_claim_job()
        with patch.object(self.social,'invoke',wraps=self.social.invoke)as called:
            self.control.execute_job(job,threading.Event());self.assertEqual([c.args[0]for c in called.call_args_list],['check'])
        result=self.store.inspection_check(values[0]['check']['id']);report=self.service.report(result['report_id'])
        self.assertEqual(report['result'],'CALLABLE');self.assertEqual(report['evidence_status'],'fixture_verified')
        self.assertEqual(report['scope']['account_version'],self.store.studio_accounts()['items'][0]['version'])
        self.assertEqual(report['actual_operations']['check'],1);self.assertEqual(report['actual_operations']['upload'],0)

    def test_timeout_keeps_previous_account_observation_and_no_new_verification(self):
        before=self.store.studio_accounts()['items'][0];check=self.begin('ACCOUNT',True)['check'];job=self.store.studio_claim_job()
        with patch.object(self.social,'invoke',side_effect=RunnerError('TIMEOUT')):self.control.execute_job(job,threading.Event())
        after=self.store.studio_accounts()['items'][0];self.assertEqual(before['state'],after['state']);self.assertEqual(before['checked_at'],after['checked_at']);self.assertGreater(after['version'],before['version'])
        report=self.service.report(self.store.inspection_check(check['id'])['report_id']);self.assertEqual(report['result'],'UNKNOWN');self.assertEqual(report['reason_code'],'CHECK_TIMEOUT');self.assertEqual(report['actual_operations']['check'],1)

    def test_definite_fixture_identity_failure_does_not_claim_live_evidence(self):
        self.social.settings['contract_profiles']['douyin']=[{'stable_subject':'different','label':'Different'}]
        check=self.begin('ACCOUNT',True)['check'];job=self.store.studio_claim_job()
        self.control.execute_job(job,threading.Event());report=self.service.report(self.store.inspection_check(check['id'])['report_id'])
        self.assertEqual(report['result'],'FAILED');self.assertEqual(report['evidence_status'],'unverified');self.assertEqual(self.store.studio_accounts()['items'][0]['state'],'EXPIRED')

    def test_manual_account_check_is_joined_without_new_job(self):
        account=self.store.studio_accounts()['items'][0];self.store.studio_check_account(account['id'],account['version'],uid())
        check=self.begin('ACCOUNT',True);self.assertTrue(check['joined'])
        with self.store.transaction()as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM studio_jobs WHERE kind='check'AND state='READY'").fetchone()[0],1)
        self.control.execute_job(self.store.studio_claim_job(),threading.Event())
        self.assertEqual(self.service.report(self.store.inspection_check(check['check']['id'])['report_id'])['result'],'CALLABLE')

    def test_unknown_account_lock_is_never_taken_or_cleared(self):
        token=uid()
        with self.store.transaction(True)as db:db.execute('INSERT INTO release_account_locks VALUES(?,?)',(self.account['id'],token))
        with self.assertRaises(Problem)as error:self.begin('ACCOUNT',True)
        self.assertEqual(error.exception.code,'CHECK_BUSY')
        with self.store.transaction()as db:self.assertEqual(db.execute('SELECT token FROM release_account_locks').fetchone()[0],token)

    def test_late_result_after_external_account_version_never_overwrites_current(self):
        check=self.begin('ACCOUNT',True)['check'];job=self.store.studio_claim_job();original=self.social.invoke
        def invoke(*args,**kwargs):
            result=original(*args,**kwargs);account=self.store.studio_accounts()['items'][0]
            self.store.studio_account_update(account['id'],'Changed',[],account['version'],uid())
            return result
        with patch.object(self.social,'invoke',side_effect=invoke):self.control.execute_job(job,threading.Event())
        self.assertEqual(self.store.studio_accounts()['items'][0]['label'],'Changed')
        result=self.store.inspection_check(check['id']);self.assertEqual(result['state'],'SUPERSEDED')
        report=self.service.report(result['report_id']);self.assertEqual(report['freshness'],'SUPERSEDED')
        self.assertIsNone(self.service.channel(self.channel('ACCOUNT')['id'])['latest_report'])

    def test_report_expires_without_rewrite_and_changed_input_supersedes(self):
        check=self.begin('MEDIA')['check'];report=self.finish(check)
        with self.store.transaction()as db:before=db.execute('SELECT payload FROM inspection_reports').fetchone()[0]
        self.clock.value+=86401;self.assertEqual(self.service.report(report['id'])['freshness'],'EXPIRED')
        self.runtime.media_settings={};self.assertEqual(self.service.report(report['id'])['freshness'],'SUPERSEDED')
        with self.store.transaction()as db:self.assertEqual(before,db.execute('SELECT payload FROM inspection_reports').fetchone()[0])

    def test_recovery_records_interruption_and_rejects_late_token(self):
        check=self.begin()['check'];job=self.store.studio_claim_job();self.store.inspection_running(job);self.store.studio_recover()
        self.assertEqual(self.store.inspection_check(check['id'])['state'],'INTERRUPTED')
        self.store.inspection_finish(job,OUTCOME)
        self.assertEqual(self.service.report(self.store.inspection_check(check['id'])['report_id'])['result'],'UNKNOWN')

    def test_expired_unstarted_check_does_not_report_any_actual_operation(self):
        check=self.begin()['check'];self.clock.value+=61;self.assertIsNone(self.store.studio_claim_job())
        report=self.service.report(self.store.inspection_check(check['id'])['report_id']);self.assertEqual(report['reason_code'],'CHECK_TIMEOUT');self.assertTrue(all(v==0 for v in report['actual_operations'].values()))

    def test_pagination_scopes_and_report_canaries(self):
        first=self.service.list(limit=1);self.assertIsNotNone(first['next_cursor']);second=self.service.list(first['next_cursor'],1);self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        with self.assertRaises(Problem):self.service.list(first['next_cursor']+'broken',1)
        check=self.begin()['check'];report=self.finish(check)
        raw=json.dumps(report)
        for private in ['credential-canary',str(self.root),'credential_ref','fingerprint']:self.assertNotIn(private,raw)
        ChannelReport.model_validate(report)

    def test_v8_migration_failure_rolls_back_without_touching_existing_facts(self):
        old=self.root/'old';old.mkdir();path=old/'state.sqlite3'
        script=''.join(getattr(migrations,'VERSION_'+n)for n in ['ONE','TWO','THREE','FOUR','FIVE','SIX','SEVEN','EIGHT'])
        with sqlite3.connect(path)as db:db.executescript(script);db.execute("INSERT INTO studio_projects VALUES('kept','{}',3,'created','updated')")
        with patch.object(migrations,'VERSION_NINE',migrations.VERSION_NINE+';INVALID SQL'):
            with self.assertRaises(sqlite3.OperationalError):Store(old)
        with sqlite3.connect(path)as db:self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0],8);self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='inspection_channels'").fetchone())
        store=Store(old)
        with store.transaction()as db:self.assertEqual(db.execute('SELECT version FROM studio_projects').fetchone()[0],3)


class SocialReadOnlyClassificationTests(unittest.TestCase):
    def test_internal_check_keeps_legacy_validity_and_separates_timeout(self):
        import tempfile
        with tempfile.TemporaryDirectory()as tmp,patch.dict(sys.modules,{'conf':ModuleType('conf')}),patch.object(social_bridge,'configure_browser'):
            request={'operation':'check','platform':'douyin','payload':{},'settings':{'source_root':tmp}}
            for exception,reason in [(TimeoutError('private-canary'),'CHECK_TIMEOUT'),(ValueError('private-canary'),'IDENTITY_UNVERIFIED'),(RuntimeError('private-canary'),'IDENTITY_UNVERIFIED')]:
                with patch.object(social_bridge,'verified_profile',new=AsyncMock(side_effect=exception)):
                    result=asyncio.run(social_bridge.execute(request,Path(tmp)))
                self.assertFalse(result['valid']);self.assertEqual(result['observation'],{'result':'UNKNOWN','reason_code':reason});self.assertNotIn('private-canary',json.dumps(result))
