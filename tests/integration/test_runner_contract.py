import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from loopcast.runner import run, RunnerError
from loopcast.generation import config_text, Generator


class RunnerTests(unittest.TestCase):
    def test_bounded_errors_and_environment(self):
        with tempfile.TemporaryDirectory() as work:
            env = Generator.environment(Path(work))
            os.environ['PUBLISH_SECRET_CANARY'] = 'must-not-leak'
            try:
                self.assertEqual(run([sys.executable, '-c', "import os; print(os.environ.get('PUBLISH_SECRET_CANARY','absent'))"], work, env), b'absent\n')
            finally:
                del os.environ['PUBLISH_SECRET_CANARY']
            for script, code, kwargs in [
                ("print('x'*10000)", 'OUTPUT_LIMIT', {'limit':100}),
                ("import time; time.sleep(10)", 'TIMEOUT', {'timeout':.1}),
                ("import time; time.sleep(10)", 'CANCELLED', {'cancelled':lambda: True}),
                ("import sys; sys.stderr.write('secret'); sys.exit(2)", 'UPSTREAM_FAILED', {}),
            ]:
                with self.assertRaises(RunnerError) as error:
                    run([sys.executable, '-c', script], work, env, **kwargs)
                self.assertEqual(str(error.exception), code)

    def test_grandchild_holding_pipes_is_killed(self):
        with tempfile.TemporaryDirectory() as work:
            pidfile = Path(work) / 'pid'
            script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open('pid','w').write(str(p.pid)); time.sleep(30)"
            with self.assertRaises(RunnerError):
                run([sys.executable,'-c',script], work, Generator.environment(Path(work)), timeout=.2)
            pid = int(pidfile.read_text())
            for _ in range(100):
                status = Path(f'/proc/{pid}/stat')
                try:
                    state = status.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == 'Z': break
                time.sleep(.01)
            else:
                self.fail('Grandchild remained alive')

    def test_config_rejects_autopublish_and_undeclared_channels(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'config.toml'
            path.touch(mode=0o600)
            for text in ['[app]\nupload_post_enabled=true', '[app]\nupload_post_auto_upload=true', '[app]\nother_publish_key="secret"', '[publish]\nkey="secret"']:
                path.write_text(text)
                with self.assertRaises(ValueError): config_text(path)
            path.write_text('[app]\nopenai_api_key="canary"')
            text = config_text(path)
            self.assertIn('upload_post_enabled = false', text)
            self.assertIn('upload_post_auto_upload = false', text)
