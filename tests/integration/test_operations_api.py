import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from studio_support import configurations
from support import ServerProcess
from test_studio_api import Owner


class OperationsAPITests(unittest.TestCase):
    def test_installed_readonly_cli_and_typed_endpoints_do_not_create_work_or_dump_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=configurations(root/'components');server=ServerProcess(root/'private-state',*paths).start()
            try:
                owner=Owner(server)
                code,status=owner.request('/v1/operations/status');self.assertEqual(code,200,status)
                self.assertTrue(status['service_ready']);self.assertTrue(status['effective_sending_enabled']);self.assertEqual(status['evidence_mode'],'contract')
                code,report=owner.request('/v1/operations/snapshot');self.assertEqual(code,200,report)
                self.assertTrue(report['runtime']['service_ready']['value']);self.assertEqual(report['database']['schema']['value'],9)
                self.assertEqual(report['media']['ffmpeg']['status'],'verified')
                self.assertEqual(report['application']['build_source']['status'],'unavailable')
                code,reconciliation=owner.request('/v1/operations/reconciliation');self.assertEqual(code,200)
                self.assertEqual(reconciliation['coverage'],'NOT_RESTORED');self.assertFalse(reconciliation['release_eligible'])
                for resource in ('status','snapshot','reconciliation'):
                    self.assertIn(owner.request('/v1/operations/'+resource,{})[0],(404,405))
                command=[sys.executable,'-I','-m','loopcast','snapshot','--data-dir',str(root/'private-state'),'--media-config',str(paths[1]),'--generator-config',str(paths[0])]
                result=subprocess.run(command,capture_output=True,text=True,cwd='/tmp',timeout=15,check=True);observed=json.loads(result.stdout)
                self.assertEqual(observed['runtime']['service_ready']['status'],'unavailable')
                self.assertEqual(observed['runtime']['owner_lock']['value'],'held')
                output=root/'report.md';subprocess.run(command+['--format','markdown','--output',str(output)],capture_output=True,text=True,cwd='/tmp',timeout=15,check=True)
                self.assertEqual(output.stat().st_mode & 0o777,0o600);self.assertTrue(output.read_text().startswith('# LoopCast observed runtime'))
                original=paths[0].read_bytes()
                rejected=subprocess.run(command+['--output',str(paths[0])],capture_output=True,text=True,cwd='/tmp',timeout=15)
                self.assertEqual(rejected.returncode,1);self.assertEqual(paths[0].read_bytes(),original)
                self.assertNotIn(str(root),rejected.stderr);self.assertNotIn('Traceback',rejected.stderr)
                content=json.dumps(report)+result.stdout+output.read_text()
                for private in ('contract-provider-canary','private-credential-canary',str(root),'credentials.json','config_ref'):self.assertNotIn(private,content)
                self.assertEqual(owner.request('/v1/runs')[1]['items'],[])
                invalid=root/'invalid.json';invalid.write_text('{')
                result=subprocess.run([sys.executable,'-I','-m','loopcast','snapshot','--data-dir',str(root/'private-state'),'--media-config',str(invalid)],capture_output=True,text=True,cwd='/tmp',timeout=15,check=True)
                self.assertEqual(json.loads(result.stdout)['media']['ffmpeg']['status'],'unavailable')
            finally:server.stop()
