import hashlib
import http.cookiejar
import json
import os
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from loopcast.domain import uid
from studio_support import configurations
from support import ServerProcess


class Owner:
    def __init__(self, server):
        self.server = server
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request('/v1/studio/capabilities')

    def request(self, path, body=None, key=None, method=None, raw=None, headers=None):
        options = {'Origin': self.server.url, **(headers or {})}
        data = raw
        if body is not None:
            options['Content-Type'] = 'application/json'; data = json.dumps(body).encode()
        if data is not None: options['Idempotency-Key'] = key or uid()
        request = urllib.request.Request(self.server.url + path, data=data, headers=options, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                content = response.read()
                return response.status, json.loads(content) if 'application/json' in response.headers.get('Content-Type','') else content
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def wait(self, path, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, value = self.request(path)
            if code == 200 and predicate(value): return value
            time.sleep(.04)
        raise AssertionError((path, code, value))

    def authorize(self, platform, subject=None, account_id=None):
        method = 'browser' if platform == 'youtube' else 'qr'
        code, session = self.request('/v1/studio/authorizations', {'platform': platform, 'method': method, 'account_id': account_id})
        assert code == 200, session
        path = '/v1/studio/authorizations/' + session['id']
        code, value = self.request(path + '/controls', {'kind': 'confirm', 'text': subject or 'test-' + platform})
        assert code == 200, value
        return self.wait(path, lambda s: s['state'] == 'SUCCEEDED')


class StudioAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        paths = configurations(self.root / 'components')
        self.server = ServerProcess(self.root / 'data', *paths).start(); self.addCleanup(self.server.stop)
        self.owner = Owner(self.server)

    def import_video(self, key=None, parent=None, content=None):
        suffix = '&parent_id=' + parent if parent else ''
        code, asset = self.owner.request('/v1/studio/assets/import?kind=video' + suffix, raw=content or (self.root/'components/sample.mp4').read_bytes(), key=key,
            headers={'Content-Type':'application/octet-stream','X-File-Name':'clip.mp4','X-Source':'Owned%20contract%20media'})
        self.assertEqual(code, 200, asset); return asset

    def test_project_revision_all_stage_outputs_and_media_delivery(self):
        asset = self.import_video('import')
        self.assertEqual(self.import_video('import')['id'], asset['id'])
        code, project = self.owner.request('/v1/studio/projects', {'title':'Contract creation'}, 'project')
        self.assertEqual(code, 200, project)
        draft = project['draft']; draft['script'] = 'First narration\n\nSecond narration'
        draft['scenes'] = [{'id':uid(),'narration':'First narration','visual':'orange landscape','duration':5}, {'id':uid(),'narration':'Second narration','visual':'orange detail','duration':3}]
        draft['settings'].update({'material_ids':[asset['id']],'voice':'en-US-AriaNeural-Female','aspect':'16:9','subtitles':False,'keywords':['safe input']})
        path = '/v1/studio/projects/' + project['id']
        code, project = self.owner.request(path, {'expected_version':1,'draft':draft}, 'save', 'PUT'); self.assertEqual(code,200,project)
        self.assertEqual(self.owner.request(path, {'expected_version':1,'draft':draft}, 'save', 'PUT')[1]['id'],project['id'])
        self.assertEqual(self.owner.request(path, {'expected_version':1,'draft':draft}, 'stale', 'PUT')[0],409)
        project = self.owner.request(path+'/revisions', {'expected_version':project['version']})[1]; revision=project['revisions'][0]
        for stage in ('script','audio','materials','subtitle','video'):
            code, project = self.owner.request(path+'/revisions/'+revision['id']+'/generate', {'expected_version':project['version'],'stage':stage})
            self.assertEqual(code,200,project); generation = project['revisions'][0]['generations'][0]
            final = self.server.wait(generation['run_id'], {'SUCCEEDED','FAILED'})
            self.assertEqual(final['state'],'SUCCEEDED',final)
            project = self.owner.request(path)[1]; current=project['revisions'][0]['generations'][0]
            self.assertEqual(current['id'],generation['id']); self.assertTrue(current['asset_ids']); self.assertEqual(len(current['component_revision']),40)
            for identity in current['asset_ids']:
                result = self.owner.request('/v1/studio/assets/'+identity)[1]
                self.assertEqual(result['generation_id'],generation['id']); self.assertTrue(result['attempt_id'])
                if result['kind'] == 'video': self.assertEqual(result['family_id'],asset['family_id'])
                code, content = self.owner.request('/v1/studio/assets/'+identity+'/content'); self.assertEqual(code,200)
                self.assertEqual(hashlib.sha256(content).hexdigest(),result['sha256'])
        headers={'Range':'bytes=0-9'}
        code, part=self.owner.request('/v1/studio/assets/'+asset['id']+'/content',headers=headers)
        self.assertEqual(code,206);self.assertEqual(len(part),10)
        changed=draft|{'script':'Changed','scenes':[]}
        project=self.owner.request(path,{'expected_version':project['version'],'draft':changed},method='PUT')[1]
        self.assertEqual(project['revisions'][0]['draft']['script'],draft['script'])
        self.server.stop();self.server.start();self.owner=Owner(self.server)
        self.assertEqual(self.owner.request(path)[1],project)
        self.assertFalse(list((self.root/'data/work').rglob('config.toml')))

    def test_owner_isolation_cancel_expiry_relogin_and_private_credentials(self):
        code,session=self.owner.request('/v1/studio/authorizations',{'platform':'douyin','method':'qr'})
        self.assertEqual(code,200,session);path='/v1/studio/authorizations/'+session['id']
        other=Owner(self.server)
        for suffix in ('','/frame'):
            self.assertEqual(other.request(path+suffix)[0],404)
        self.assertEqual(other.request(path+'/cancel',{})[0],404)
        self.assertEqual(other.request(path+'/controls',{'kind':'confirm'})[0],404)
        self.assertEqual(self.owner.request(path+'/cancel',{})[1]['state'],'CANCELLED')
        first=self.owner.authorize('douyin');again=self.owner.authorize('douyin',account_id=first['account_id'])
        self.assertEqual(first['account_id'],again['account_id'])
        public=self.owner.request('/v1/studio/accounts')[1]
        self.assertEqual(len(public['items']),1)
        account=public['items'][0];self.assertEqual(account['state'],'AVAILABLE')
        data=json.dumps(public)+json.dumps(self.owner.request('/v1/studio/authorizations')[1])
        self.assertNotIn('private-credential-canary',data);self.assertNotIn('credential_ref',data)
        checked=self.owner.request('/v1/studio/accounts/'+account['id']+'/check',{'expected_version':account['version']})[1]
        self.assertEqual(checked['state'],'PENDING')
        self.owner.wait('/v1/studio/accounts',lambda x:x['items'][0]['state']=='AVAILABLE')
        for path in (self.root/'data/social/credentials').glob('*'):
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        logs='\n'.join(self.server.diagnostics)
        for secret in ('private-credential-canary','contract-provider-canary','credentials.json','cookie_info'):
            self.assertNotIn(secret,logs)
        code,session=self.owner.request('/v1/studio/authorizations',{'platform':'youtube','method':'browser'})
        self.owner.wait('/v1/studio/authorizations/'+session['id'],lambda x:x['state']=='EXPIRED',timeout=13)

    def test_per_target_receipts_query_permanent_copy_guard_and_restart(self):
        asset=self.import_video()
        accounts=[]
        for platform in ('douyin','xiaohongshu','bilibili','youtube'):
            session=self.owner.authorize(platform);accounts.append(next(a for a in self.owner.request('/v1/studio/accounts')[1]['items'] if a['id']==session['account_id']))
        value={'asset_id':asset['id'],'title':'Contract release','description':'Actual registered media','cover_id':None,
               'targets':[{'account_id':a['id'],'fields':{'category':1 if a['platform']=='bilibili' else None,'visibility':'public','declaration':None}} for a in accounts]}
        code,check=self.owner.request('/v1/studio/releases/preflight',value);self.assertEqual(code,200,check);self.assertTrue(check['allowed'],check)
        create=value|{'preflight_digest':check['digest'],'confirmed':True}
        code,batch=self.owner.request('/v1/studio/releases',create,'release');self.assertEqual(code,200,batch)
        self.assertEqual(self.owner.request('/v1/studio/releases',create,'release')[1],batch)
        base='/v1/studio/releases/'+batch['id']
        # Observe partial status before unresolved targets join the batch.
        target=batch['targets'][0]
        self.owner.request(base+'/targets/'+target['id']+'/dispatch',{'expected_version':target['version']})
        batch=self.owner.wait(base,lambda b:b['targets'][0]['state']=='PUBLISHED');self.assertEqual(batch['state'],'PARTIAL')
        for target in batch['targets'][1:]:
            self.owner.request(base+'/targets/'+target['id']+'/dispatch',{'expected_version':target['version']})
        batch=self.owner.wait(base,lambda b:[t['state'] for t in b['targets']]==['PUBLISHED','UNKNOWN','NOT_SENT','SUBMITTED'])
        self.assertEqual(batch['state'],'UNRESOLVED')
        unknown=batch['targets'][1];endpoint=base+'/targets/'+unknown['id']
        self.assertEqual(self.owner.request(endpoint+'/dispatch',{'expected_version':unknown['version']})[0],409)
        self.assertFalse(unknown['can_dispatch']);self.assertTrue(unknown['can_verify'])
        self.owner.request(endpoint+'/verify',{'expected_version':unknown['version']})
        batch=self.owner.wait(base,lambda b:b['targets'][1]['state']=='PUBLISHED')
        self.assertEqual(len(batch['targets'][1]['attempts']),1);self.assertEqual(len(batch['targets'][1]['receipts']),2)
        self.assertTrue(batch['targets'][1]['receipts'][-1]['work_id'])
        self.assertTrue(all(t['evidence_mode']=='contract' for t in batch['targets']))
        copy=self.import_video();self.assertEqual(copy['family_id'],asset['family_id'])
        duplicate=value|{'asset_id':copy['id'],'title':'Another title','targets':value['targets'][:1]}
        check=self.owner.request('/v1/studio/releases/preflight',duplicate)[1];self.assertIn('PERMANENT_DUPLICATE',check['targets'][0]['reasons'])
        first=accounts[0];self.owner.authorize('douyin',account_id=first['id'])
        self.server.stop();self.server.start();self.owner=Owner(self.server)
        check=self.owner.request('/v1/studio/releases/preflight',duplicate)[1];self.assertIn('PERMANENT_DUPLICATE',check['targets'][0]['reasons'])
        persisted=self.owner.request(base)[1];self.assertEqual(persisted['targets'][0]['state'],'PUBLISHED')
        self.assertEqual(persisted['targets'][3]['state'],'SUBMITTED')
        self.assertFalse(self.owner.request('/v1/capabilities')[1]['real_platform_sending'])

    def test_strict_new_contract_does_not_relax_old_inputs(self):
        for body in ({'title':None},{'title':True},{'title':'x','unknown':1}):
            self.assertEqual(self.owner.request('/v1/studio/projects',body)[0],400)
        for body in ({'mode':'fixture','delay_ms':True},{'mode':'fixture','title':None}):
            self.assertEqual(self.owner.request('/v1/runs',body)[0],400)
        self.assertEqual(self.owner.request('/v1/studio/authorizations',{'platform':'youtube','method':'qr'})[0],422)
        self.assertEqual(self.owner.request('/v1/studio/assets/import?kind=video',raw=b'not a video',headers={'Content-Type':'application/octet-stream','X-Source':'own'})[0],400)

    def test_transcode_and_declared_edit_keep_media_lineage(self):
        original = self.import_video()
        tools = Path(os.environ['LOOPCAST_MEDIA_TOOLS'])
        transcode = self.root/'transcode.mp4'
        subprocess.run([str(tools/'ffmpeg'),'-v','error','-i',str(self.root/'components/sample.mp4'),'-vf','scale=128:128','-an','-c:v','libvpx-vp9','-metadata','title=transcoded',str(transcode)],check=True)
        converted = self.import_video(content=transcode.read_bytes())
        self.assertNotEqual(original['sha256'],converted['sha256'])
        self.assertEqual(original['family_id'],converted['family_id'])
        distinct = self.root/'distinct.mp4'
        subprocess.run([str(tools/'ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=64x64:d=0.2','-an','-c:v','libvpx-vp9',str(distinct)],check=True)
        independent = self.import_video(content=distinct.read_bytes())
        self.assertNotEqual(original['family_id'],independent['family_id'])
        derived = self.import_video(content=distinct.read_bytes(),parent=original['id'])
        session=self.owner.authorize('douyin')
        value={'asset_id':original['id'],'title':'Lineage','targets':[{'account_id':session['account_id']}]}
        check=self.owner.request('/v1/studio/releases/preflight',value)[1]
        self.assertTrue(check['allowed'],check)
        batch=self.owner.request('/v1/studio/releases',value|{'confirmed':True,'preflight_digest':check['digest']})[1]
        target=batch['targets'][0];base='/v1/studio/releases/'+batch['id']
        self.owner.request(base+'/targets/'+target['id']+'/dispatch',{'expected_version':target['version']})
        self.owner.wait(base,lambda b:b['targets'][0]['state']=='PUBLISHED')
        for asset in (converted,independent,derived):
            check=self.owner.request('/v1/studio/releases/preflight',value|{'asset_id':asset['id']})[1]
            self.assertIn('PERMANENT_DUPLICATE',check['targets'][0]['reasons'])
