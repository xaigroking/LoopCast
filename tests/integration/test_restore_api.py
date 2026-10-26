import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from asgi_support import ASGIThread
from studio_support import configurations
from test_studio_api import Owner
from loopcast.social import SocialAdapter


class RestoreAPITests(unittest.TestCase):
    def test_installed_old_backup_barrier_blocks_api_startup_queue_and_relogin(self):
        for outcome in ('published', 'lost_response'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); paths = configurations(root / 'components')
                media = json.loads(paths[1].read_text()); social = json.loads(paths[2].read_text()); social['contract_results']['test-douyin'] = outcome
                calls = []; invoke = SocialAdapter.invoke
                def observe(adapter, operation, *args, **kwargs):
                    if operation == 'upload':calls.append((adapter.root, operation))
                    return invoke(adapter, operation, *args, **kwargs)
                def command(*args):
                    completed = subprocess.run([sys.executable, '-I', '-m', 'loopcast', *map(str,args)], capture_output=True, text=True, cwd='/tmp', timeout=20, check=True)
                    return json.loads(completed.stdout)
                with patch.object(SocialAdapter, 'invoke', observe):
                    server = ASGIThread(root / 'source', media_settings=media, social_settings=social).start()
                    try:
                        owner = Owner(server); account = owner.authorize('douyin')['account_id']
                        code, asset = owner.request('/v1/studio/assets/import?kind=video', raw=(root / 'components/sample.mp4').read_bytes(), headers={'Content-Type':'application/octet-stream','X-Source':'Controlled restore fixture'})
                        self.assertEqual(code, 200, asset)
                        value = {'asset_id':asset['id'],'title':'Restore fixture','description':'','cover_id':None,'targets':[{'account_id':account,'fields':{'category':None,'visibility':'public','declaration':None}}]}
                        preflight = owner.request('/v1/studio/releases/preflight', value)[1]
                        batch = owner.request('/v1/studio/releases', value | {'preflight_digest':preflight['digest'],'confirmed':True})[1]
                        target = batch['targets'][0]; endpoint = '/v1/studio/releases/' + batch['id'] + '/targets/' + target['id'] + '/dispatch'
                    finally:server.stop()
                    backup = command('backup','create','--data-dir',root/'source','--output',root/'backup')
                    server = ASGIThread(root/'source',media_settings=media,social_settings=social).start()
                    try:
                        owner = Owner(server); self.assertEqual(owner.request(endpoint,{'expected_version':target['version']})[0],200)
                        final = owner.wait('/v1/studio/releases/'+batch['id'],lambda b:b['targets'][0]['state'] in {'PUBLISHED','UNKNOWN'})
                        self.assertEqual(final['targets'][0]['state'],'PUBLISHED' if outcome=='published' else 'UNKNOWN');self.assertEqual(len(calls),1)
                    finally:server.stop()
                    restored = root/'restored'
                    result = command('restore','--backup',root/'backup','--expected-manifest-sha256',backup['manifest_sha256'],'--data-dir',restored)
                    self.assertEqual(result['state'],'RESTORED_GUARDED')
                    for restart in range(2):
                        server = ASGIThread(restored,media_settings=media,social_settings=social).start()
                        try:
                            owner = Owner(server); self.assertFalse(owner.request('/v1/studio/capabilities')[1]['sending_enabled'])
                            code, denied = owner.request(endpoint,{'expected_version':target['version']})
                            self.assertEqual((code,denied['error']['code']),(409,'RESTORE_GUARD_ACTIVE'))
                            if restart==0:
                                self.assertEqual(owner.authorize('douyin',account_id=account)['account_id'],account)
                            self.assertEqual(owner.request(endpoint,{'expected_version':target['version']})[0],409)
                            store = server.runtime.store
                            with store.transaction(True) as db:
                                row = db.execute('SELECT context FROM studio_jobs LIMIT 1').fetchone()
                                db.execute("INSERT INTO studio_jobs VALUES('restored-queued-upload','dispatch',?,'READY','old-token',9999999999,?, 'created') ON CONFLICT(id) DO NOTHING",(target['id'],row[0]))
                            time.sleep(.2);self.assertIsNone(store.studio_claim_job());self.assertEqual(len(calls),1)
                            report = command('reconcile','--data-dir',restored)
                            self.assertFalse(report['release_eligible']);self.assertEqual(report['coverage'],'INCONCLUSIVE')
                            self.assertEqual(report['targets'][0]['query'],'unavailable')
                            code, content = owner.request('/v1/studio/assets/'+asset['id']+'/content');self.assertEqual(code,200);self.assertEqual(content,(root/'components/sample.mp4').read_bytes())
                        finally:server.stop()
