import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from support import ServerProcess


class BackupCLITests(unittest.TestCase):
    def test_installed_backup_requires_stopped_owner_and_verifies_pinned_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'private-path-canary'; output = root / 'backup'
            server = ServerProcess(source).start()
            try:
                _, run = server.request('/v1/runs', {'title': 'Original fixture', 'delay_ms': 0}, 'backup-fixture')
                done = server.wait(run['run_id'], {'SUCCEEDED'})
                command = [sys.executable, '-I', '-m', 'loopcast', 'backup', 'create', '--data-dir', str(source), '--output', str(output)]
                denied = subprocess.run(command, capture_output=True, text=True, cwd='/tmp', timeout=10)
                self.assertEqual(denied.returncode, 1); self.assertFalse(output.exists())
                self.assertNotIn(str(source), denied.stderr); self.assertNotIn('Traceback', denied.stderr)
                server.stop()
                created = subprocess.run(command, capture_output=True, text=True, cwd='/tmp', timeout=15, check=True)
                report = json.loads(created.stdout); self.assertEqual(report['state'], 'VERIFIED_PRIVATE_BACKUP')
                verify = [sys.executable, '-I', '-m', 'loopcast', 'backup', 'verify', '--backup', str(output), '--expected-manifest-sha256', report['manifest_sha256']]
                checked = subprocess.run(verify, capture_output=True, text=True, cwd='/tmp', timeout=10, check=True)
                self.assertEqual(json.loads(checked.stdout)['backup_id'], report['backup_id'])
                server.start(); self.assertEqual(server.request('/v1/runs/' + run['run_id'])[1], done)
                denied = subprocess.run(command, capture_output=True, text=True, cwd='/tmp', timeout=10)
                self.assertEqual(denied.returncode, 1)
                (output / 'state.sqlite3').write_bytes(b'invalid')
                bad = subprocess.run(verify, capture_output=True, text=True, cwd='/tmp', timeout=10)
                self.assertEqual(bad.returncode, 1); self.assertNotIn('Traceback', bad.stderr)
            finally:
                server.stop()
