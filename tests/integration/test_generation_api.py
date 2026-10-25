import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from generation_support import settings
from support import ServerProcess


class GenerationApiTests(unittest.TestCase):
    def test_installed_generation_and_restart_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config=settings(directory);path=Path(directory)/'generator.json';path.write_text(json.dumps(config))
            data=Path(directory)/'data';server=ServerProcess(data,path).start()
            try:
                self.assertTrue(server.request('/v1/capabilities')[1]['video_generation'])
                code,run=server.request('/v1/runs',{'mode':'mpt','title':'orange video'},'video')
                self.assertEqual(code,202)
                final=server.wait(run['run_id'],{'SUCCEEDED','FAILED'})
                self.assertEqual(final['state'],'SUCCEEDED',final)
                self.assertEqual(final['mode'],'mpt');self.assertEqual(len(final['media']),1)
                artifact=final['artifacts'][0]
                content=server.request('/v1/artifacts/'+artifact['id']+'/content')[1]
                self.assertEqual(hashlib.sha256(content).hexdigest(),artifact['sha256'])
                self.assertFalse(any((data/'work').rglob('config.toml')))
                entries = [json.loads(line) for line in server.diagnostics if line.startswith('{')]
                adapter = next(entry for entry in entries if entry['event'] == 'adapter.completed')
                worker = next(entry for entry in entries if entry['event'] == 'attempt.started')
                self.assertEqual(adapter['result'], 'SUCCEEDED')
                self.assertEqual(adapter['run_id'], run['run_id'])
                self.assertEqual(adapter['trace_id'], worker['trace_id'])
                self.assertEqual(adapter['request_id'], worker['request_id'])
                self.assertEqual(adapter['parent_span_id'], worker['span_id'])
                server.stop();server.start()
                replay=server.request('/v1/runs',{'mode':'mpt','title':'orange video'},'video')[1]
                self.assertEqual(replay,final)
            finally:server.stop()
