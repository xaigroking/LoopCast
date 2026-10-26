"""Installed HTTP inspections execute only declared read-only operations."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from loopcast.domain import uid
from loopcast.generation import Generator
from loopcast.creation import CreationExecutor
from loopcast.inspection_models import InspectionSettings, ChannelReport
from asgi_support import ASGIThread
from studio_support import configurations
from test_studio_api import Owner


class InspectionAPITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        paths=configurations(self.root/'components')
        self.generator=json.loads(paths[0].read_text());self.media=json.loads(paths[1].read_text())
        self.server=ASGIThread(self.root/'data',generator_settings=self.generator,media_settings=self.media,social_settings=json.loads(paths[2].read_text())).start()
        self.addCleanup(self.server.stop);self.owner=Owner(self.server)

    def channel(self, kind):
        status,value=self.owner.request('/v1/operations/channels');self.assertEqual(status,200,value)
        return next(c for c in value['items']if c['kind']==kind)

    def check(self, channel, online=False,key=None):
        code,value=self.owner.request('/v1/operations/channels/'+channel['id']+'/checks',{'expected_version':channel['channel_version'],'online':online,'confirmed_online':online},key)
        self.assertEqual(code,202,value)
        final=self.owner.wait('/v1/operations/channel-checks/'+value['check']['id'],lambda c:c['state']not in {'QUEUED','RUNNING'},timeout=15)
        self.assertTrue(final['report_id'],final);code,report=self.owner.request('/v1/operations/channel-reports/'+final['report_id']);self.assertEqual(code,200,report);ChannelReport.model_validate(report)
        return value,report

    def test_local_real_processes_http_contract_and_zero_forbidden_operations(self):
        social=self.server.runtime.social
        with patch.object(Generator,'generate',side_effect=AssertionError('Forbidden generation'))as generate,patch.object(CreationExecutor,'execute',side_effect=AssertionError('Forbidden creation'))as create,patch.object(social,'invoke',side_effect=AssertionError('No platform operation'))as platform:
            for kind in ('CONTROL_STORAGE','GENERATOR','MEDIA'):
                first,report=self.check(self.channel(kind),key='local-'+kind)
                self.assertEqual(report['result'],'CALLABLE',report)
                self.assertEqual(report['freshness'],'FRESH')
                self.assertEqual(report['evidence_status'],'source_inspected')
                for operation in ('generate','paid_model','authorize','upload','query','check'):self.assertEqual(report['actual_operations'][operation],0)
                if kind=='GENERATOR':self.assertEqual(report['actual_operations']['cli_help_process'],1);self.assertEqual(report['actual_operations']['version_process'],2)
                channel=self.channel(kind);code,replayed=self.owner.request('/v1/operations/channels/'+channel['id']+'/checks',{'expected_version':1,'online':False,'confirmed_online':False},'local-'+kind)
                self.assertEqual(code,202,replayed);self.assertEqual(replayed['check']['id'],first['check']['id'])
            generate.assert_not_called();create.assert_not_called();platform.assert_not_called()
        self.assertEqual(self.owner.request('/v1/runs')[1]['items'],[])
        self.assertEqual(len(self.server.app.openapi()['paths']),63)

    def test_strict_requests_readonly_get_invalid_cursor_and_stale_component(self):
        channel=self.channel('GENERATOR');path='/v1/operations/channels/'+channel['id']+'/checks'
        for body in ({'expected_version':True},{'expected_version':1,'online':1},{'expected_version':1,'online':True},{'expected_version':1,'command':'private-canary'}):
            code,value=self.owner.request(path,body);self.assertEqual(code,400,value);self.assertNotIn('private-canary',json.dumps(value))
        self.assertEqual(self.owner.request(path,{'expected_version':1,'online':True,'confirmed_online':True})[0],422)
        first,report=self.check(channel)
        original=self.server.runtime.generator_settings
        self.server.runtime.generator_settings=original|{'manifest':original['manifest']|{'artifact_sha256':'0'*64}}
        current=self.channel('GENERATOR');self.assertGreater(current['channel_version'],channel['channel_version']);self.assertEqual(current['latest_report']['freshness'],'SUPERSEDED')
        self.assertEqual(self.owner.request(path,{'expected_version':1})[0],409)
        _,invalid=self.check(current);self.assertEqual(invalid['result'],'MISCONFIGURED');self.assertEqual(self.owner.request('/v1/operations/channel-reports/'+report['id'])[1]['freshness'],'SUPERSEDED')
        self.assertEqual(self.owner.request('/v1/operations/channels?cursor=bad')[0],400)
        self.assertEqual(self.owner.request('/v1/operations/channels?limit=0')[0],400)
        serialized=json.dumps(invalid)
        for value in (str(self.root),'contract-provider-canary','private-credential-canary','credential_ref'):self.assertNotIn(value,serialized)

    def test_account_policy_and_actual_check_preserve_restore_barrier(self):
        self.owner.authorize('douyin');channel=self.channel('ACCOUNT');path='/v1/operations/channels/'+channel['id']+'/checks'
        self.assertEqual(self.owner.request(path,{'expected_version':channel['channel_version'],'online':True,'confirmed_online':True})[0],422)
        _,passive=self.check(channel);self.assertEqual(passive['reason_code'],'ONLINE_CHECK_NOT_REQUESTED');self.assertEqual(passive['actual_operations']['check'],0)
        account_id=channel['scope']['account_id'];self.server.runtime.store.inspection_settings=InspectionSettings(online_accounts=[account_id])
        store=self.server.runtime.store
        with store.transaction(True)as db:
            restore=uid();db.execute('INSERT INTO restore_events VALUES(?,?,?,?,?,?,?,?)',(restore,uid(),'a'*64,'created','restored',8,store._execution_epoch(db),'ALL_STUDIO_UPLOADS'))
            db.execute("INSERT INTO restore_guard VALUES(1,?,1,'ACTIVE','EXTERNAL_HISTORY_UNRECONCILED')",(restore,))
        social=self.server.runtime.social
        with patch.object(social,'invoke',wraps=social.invoke)as invoked:
            _,report=self.check(self.channel('ACCOUNT'),True)
            self.assertEqual([c.args[0]for c in invoked.call_args_list],['check'])
        self.assertEqual(report['evidence_status'],'fixture_verified');self.assertEqual(report['actual_operations']['check'],1)
        self.assertFalse(self.owner.request('/v1/operations/status')[1]['upload_allowed_by_recovery'])
        self.assertFalse(next(c for c in report['declarations']if c['operation']=='native_schedule')['supported'])

    def test_total_probe_timeout_reaps_nested_cli_process(self):
        import os
        import signal
        import generation_support
        marker=self.root/'diagnostic-child.pid'
        launcher=generation_support.LAUNCHER.replace('def main():',"def main():\n import time\n Path("+repr(str(marker))+").write_text(str(os.getpid()))\n time.sleep(20)")
        with patch.object(generation_support,'LAUNCHER',launcher):paths=configurations(self.root/'slow-components')
        self.server.runtime.generator_settings=json.loads(paths[0].read_text())
        self.server.runtime.store.inspection_settings=InspectionSettings(timeout_seconds=5)
        pid=None
        try:
            _,report=self.check(self.channel('GENERATOR'))
            self.assertEqual(report['result'],'UNKNOWN');self.assertEqual(report['reason_code'],'CHECK_TIMEOUT')
            self.assertTrue(marker.exists());pid=int(marker.read_text())
            proc=Path('/proc')/str(pid)/'stat'
            if proc.exists():self.assertEqual(proc.read_text().rsplit(')',1)[1].split()[0],'Z','Diagnostic descendant remains running')
            else:
                with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        finally:
            if pid is None and marker.exists():pid=int(marker.read_text())
            if pid is not None:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass

    def test_history_events_and_explicit_cooldown_http_contract(self):
        channel=self.channel('GENERATOR');_,report=self.check(channel)
        path='/v1/operations/channels/'+channel['id']
        code,history=self.owner.request(path+'/reports');self.assertEqual(code,200);self.assertEqual(history['items'][0]['id'],report['id'])
        code,events=self.owner.request('/v1/operations/channel-events?channel_id='+channel['id']);self.assertEqual(code,200);self.assertEqual(events['items'][0]['current_report_id'],report['id'])
        self.assertEqual(events['items'][0]['kind'],'INITIAL_OBSERVATION')
        self.server.runtime.generator_settings=self.generator|{'manifest':self.generator['manifest']|{'artifact_sha256':'0'*64}}
        channel=self.channel('GENERATOR');_,failed=self.check(channel)
        self.assertEqual(failed['result'],'MISCONFIGURED')
        code,error=self.owner.request(path+'/checks',{'expected_version':channel['channel_version']})
        self.assertEqual(code,429,error);self.assertGreater(error['error']['retry_after_seconds'],0)
        code,value=self.owner.request('/v1/operations/channel-events?cursor=invalid');self.assertEqual(code,400,value)
        self.assertEqual(self.owner.request('/v1/operations/channels')[0],200)
