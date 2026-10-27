import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.error
import uuid
from loopcast.desktop_guard import create
from loopcast.desktop_session import proof
from loopcast.platform_info import source_revision

def line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(8): raise AssertionError('No bounded managed signal')
        return process.stdout.readline().decode().strip()

class ManagedServiceTests(unittest.TestCase):
    def launch(self, root, extra=()):
        child = subprocess.Popen([sys.executable, '-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', str(root), '--managed-stdin', *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def cleanup():
            if child.poll() is None: child.kill(); child.wait(5)
            for pipe in [child.stdin, child.stdout]: pipe.close()
        self.addCleanup(cleanup); return child

    def test_real_backend_stop_eof_and_invalid_commands(self):
        for command, expected in [(b'STOP\n', 0), (None, 0), (b'INVALID\n', 1), (b'x'*600, 1), (b'STO', 1)]:
            with self.subTest(command=command[:12] if command else 'EOF'), tempfile.TemporaryDirectory() as root:
                child = self.launch(root)
                self.assertTrue(line(child).startswith('READY http://127.0.0.1:'))
                if command is not None: child.stdin.write(command); child.stdin.flush()
                child.stdin.close()
                output = child.stdout.read().decode(); self.assertEqual(child.wait(8), expected)
                self.assertEqual('STOPPED' in output, expected == 0)

    def test_real_desktop_hmac_and_all_direct_routes_require_capability(self):
        with tempfile.TemporaryDirectory() as root:
            identity = uuid.uuid4().hex; create(root, identity)
            origin = 'http://127.0.0.1:18786'; token = 'a'*64
            child = self.launch(root, ['--desktop-session-stdin', '--desktop-guard-identity', identity, '--public-origin', origin, '--trusted-proxy', '127.0.0.1/32'])
            frame = json.dumps({'format': 'loopcast.desktop-session.v1', 'token': token}).encode()+b'\n'
            child.stdin.write(frame[:19]); child.stdin.flush(); child.stdin.write(frame[19:]); child.stdin.flush()
            api = line(child).removeprefix('READY ')
            headers = {'Host': origin.removeprefix('http://'), 'X-Forwarded-Host': origin.removeprefix('http://'), 'X-Forwarded-Proto': 'http'}
            for path in ['/healthz', '/openapi.json', '/v1/runs', '/v1/unknown', '/v1/studio/assets/unknown/content']:
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(urllib.request.Request(api+path, headers=headers), timeout=3)
                self.assertEqual(error.exception.code, 403); error.exception.close()
            nonce = 'b'*64; signature = proof(token, 'request', nonce, api, origin, source_revision())
            request = urllib.request.Request(api+'/healthz', headers={**headers, 'X-LoopCast-Desktop-Probe': nonce, 'X-LoopCast-Desktop-Proof': signature})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertTrue(json.load(response)['ready'])
                self.assertEqual(response.headers['x-loopcast-desktop-proof'], proof(token, 'response', nonce, api, origin, source_revision()))
            for direction, probe, bound_origin, revision in [('response', nonce, origin, source_revision()), ('request', 'c'*64, origin, source_revision()), ('request', nonce, origin+'0', source_revision()), ('request', nonce, origin, 'd'*40)]:
                invalid = proof(token, direction, probe, api, bound_origin, revision)
                bad = urllib.request.Request(api+'/healthz', headers={**headers, 'X-LoopCast-Desktop-Probe': nonce, 'X-LoopCast-Desktop-Proof': invalid})
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(bad, timeout=3)
                self.assertEqual(error.exception.code, 403); error.exception.close()
            child.stdin.write(b'STOP\n'); child.stdin.close()
            self.assertIn('STOPPED', child.stdout.read().decode()); self.assertEqual(child.wait(8), 0)
            self.assertTrue((Path(root)/'desktop-runtime.guard.v1').exists())

    def test_malformed_session_does_not_open_a_business_owner(self):
        for raw in [b'{}\n', b'{"format":"loopcast.desktop-session.v1","token":"bad"}\n', b'x'*600, b'{"token":']:
            with tempfile.TemporaryDirectory() as root:
                identity = uuid.uuid4().hex; create(root, identity)
                child = self.launch(root, ['--desktop-session-stdin', '--desktop-guard-identity', identity, '--public-origin', 'http://127.0.0.1:18786'])
                child.stdin.write(raw); child.stdin.close()
                self.assertEqual(child.stdout.read(), b''); self.assertNotEqual(child.wait(8), 0)
                self.assertFalse((Path(root)/'state.sqlite3').exists())
