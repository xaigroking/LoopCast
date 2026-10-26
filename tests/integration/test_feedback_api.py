from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from studio_support import configurations
from test_studio_api import Owner
from support import ServerProcess
from loopcast.storage import Store


class FeedbackAPITests(unittest.TestCase):
    def test_installed_import_binding_quarantine_decision_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=configurations(root/'components');server=ServerProcess(root/'data',*paths).start()
            try:
                owner=Owner(server);session=owner.authorize('douyin');account=session['account_id']
                code,asset=owner.request('/v1/studio/assets/import?kind=video',raw=(root/'components/sample.mp4').read_bytes(),headers={'Content-Type':'application/octet-stream','X-Source':'Owned contract media'})
                self.assertEqual(code,200,asset)
                document={'format':'loopcast.metrics.v1','source':'Manual JSON fixture','evidence_mode':'contract','rows':[{
                    'platform':'douyin','stable_subject':'test-douyin','work_id':'work-verified-by-user','asset_id':asset['id'],
                    'observed_at':datetime.now(timezone.utc).isoformat(),'aggregation':'cumulative','definition':'platform_reported_v1','metrics':{'views':300,'likes':None}}]}
                raw=json.dumps(document,indent=4).encode();headers={'Content-Type':'application/json','X-File-Name':'actual-file.json'}
                code,report=owner.request('/v1/feedback/imports',raw=raw,key='import-one',headers=headers);self.assertEqual(code,200,report)
                self.assertEqual(report['rows'][0]['state'],'QUARANTINED');self.assertEqual(report['sha256'],hashlib.sha256(raw).hexdigest())
                with owner.opener.open(server.url+'/v1/feedback/imports/'+report['id']+'/original') as response:
                    self.assertEqual(response.read(),raw)
                binding={'account_id':account,'asset_id':asset['id'],'work_id':'work-verified-by-user','published_at':(datetime.now(timezone.utc)-timedelta(hours=48)).isoformat(),'source':'User checked manual fixture','confirmed':True}
                code,work=owner.request('/v1/feedback/works',binding);self.assertEqual(code,200,work);self.assertEqual(work['evidence_kind'],'MANUAL_ASSOCIATION')
                report=owner.request('/v1/feedback/imports/'+report['id']+'/reprocess',{'expected_version':1})[1]
                snapshot=report['rows'][0]['snapshot_id'];self.assertIsNotNone(snapshot)
                self.assertEqual(owner.request('/v1/feedback/imports',raw=raw,key='repeat-file',headers=headers)[1]['id'],report['id'])
                state=owner.request('/v1/feedback')[1];self.assertEqual(len(state['snapshots']),1)
                self.assertEqual(owner.request('/v1/studio/releases')[1]['items'],[])
                proposal={'snapshot_ids':[snapshot],'topic':'Next editorial round','goal':'views','minimum_age_hours':24,'minimum_views':100}
                code,decision=owner.request('/v1/feedback/decisions',proposal);self.assertEqual(code,200,decision);self.assertEqual(decision['reason'],'TOP_OBSERVED_VIEWS')
                confirm={'expected_version':1,'topic':'Reviewed new topic','brief':'User-confirmed follow-up','confirmed':True}
                code,decision=owner.request('/v1/feedback/decisions/'+decision['id']+'/confirm',confirm);self.assertEqual(code,200,decision)
                revision=decision['revisions'][0];project=owner.request('/v1/studio/projects/'+revision['project_id'])[1]
                self.assertEqual(project['revisions'][0]['decision_id'],decision['id']);self.assertEqual(project['draft']['title'],'Reviewed new topic')
                self.assertEqual(owner.request('/v1/runs')[1]['items'],[])
                server.stop();server.start();owner=Owner(server)
                self.assertEqual(owner.request('/v1/feedback')[1]['snapshots'],state['snapshots'])
                self.assertEqual(owner.request('/v1/feedback/decisions')[1][0],decision)
                invalid=document|{'rows':[document['rows'][0]|{'metrics':{'views':True}}]}
                self.assertEqual(owner.request('/v1/feedback/imports',invalid)[0],400)
                live=document|{'evidence_mode':'live'}
                code,blocked=owner.request('/v1/feedback/imports',live);self.assertEqual(code,200);self.assertEqual(blocked['rows'][0]['code'],'WORK_UNBOUND')
                self.assertEqual(len(owner.request('/v1/feedback')[1]['snapshots']),1)
            finally:server.stop()

    def test_installed_fixture_two_round_tool_retains_reference_and_duplicate_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=configurations(root/'components');data=root/'fixture-data'
            command=[sys.executable,'-I','-m','loopcast','demo-feedback','--data-dir',str(data),'--media-config',str(paths[1])]
            completed=subprocess.run(command,capture_output=True,text=True,timeout=30,check=True,cwd='/tmp');result=json.loads(completed.stdout)
            self.assertEqual(result['evidence'],'fixture_only_not_live_evidence');self.assertEqual(len(result['rounds']),2)
            store=Store(data);workspace=store.feedback_workspace();self.assertEqual(len(workspace['snapshots']),2);self.assertEqual(len(workspace['imports']),2)
            first,second=result['rounds'];self.assertEqual(result['decision']['selected_snapshot_id'],first['snapshot_id'])
            project=store.studio_project(second['project_id']);self.assertEqual(project['revisions'][0]['decision_id'],result['decision']['id'])
            self.assertNotEqual(first['asset_sha256'],second['asset_sha256'])
            for item in result['rounds']:
                batch=store.studio_release(item['release_id']);self.assertEqual(batch['state'],'PUBLISHED');self.assertEqual(batch['targets'][0]['evidence_mode'],'contract')
            denied=subprocess.run(command,capture_output=True,text=True,timeout=5,cwd='/tmp');self.assertEqual(denied.returncode,1)
            self.assertEqual(len(Store(data).feedback_workspace()['snapshots']),2)
