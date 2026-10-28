import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from loopcast import desktop_admission as admission
from loopcast.ownership import DirectoryLock


class ApplicationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'application'; self.other = Path(self.temp.name) / 'other-workspace'
        self.other.mkdir(mode=0o700); self.capability = os.urandom(32).hex(); self.operation = os.urandom(16).hex()
        for target, value in [('state_path', lambda: self.root), ('_capability', None), ('_lease_capability', None)]:
            active = patch.object(admission, target, value); active.start(); self.addCleanup(active.stop)

    def test_global_pending_fences_a_different_directory_and_never_accepts_identity_as_capability(self):
        admission.begin(self.operation, self.capability)
        with self.assertRaisesRegex(ValueError, 'HANDOFF_UNCONFIRMED'): DirectoryLock(self.other)
        self.assertFalse((self.other / 'service.lock').exists())
        with self.assertRaisesRegex(ValueError, 'HANDOFF_UNCONFIRMED'): admission.finish(self.operation, self.operation)
        with patch.object(admission, '_capability', self.capability):
            with DirectoryLock(self.other): pass
            admission.finish(self.operation, self.capability)
            with self.assertRaisesRegex(ValueError, 'EXPIRED'): DirectoryLock(self.other)
        with DirectoryLock(self.other): pass

    def test_private_bootstrap_rejects_replayed_capabilities_duplicate_fields_and_extra_keys(self):
        admission.begin(self.operation, self.capability)
        def frame(raw):
            read_fd, write_fd = os.pipe(); os.write(write_fd, raw); os.close(write_fd)
            try: return admission.load_private(read_fd)
            finally: os.close(read_fd)
        raw = json.dumps({'format': admission.BOOTSTRAP, 'capability': self.capability, 'lease': None}).encode()
        self.assertEqual(frame(raw), raw)
        admission.finish(self.operation, self.capability)
        with self.assertRaisesRegex(ValueError, 'EXPIRED'): frame(raw)
        for broken in [b'{"format":"x","format":"y","capability":null,"lease":null}', raw[:-1] + b',"extra":true}']:
            with self.assertRaises(ValueError): frame(broken)

    def test_active_lease_uses_a_real_flock_and_only_normal_stop_removes_its_marker(self):
        request = (json.dumps({'format': 'loopcast.application-lease-request.v1', 'capability': self.capability}) + '\n').encode()
        class Input:
            def __init__(inner): inner.first = True; inner.buffer = inner
            def readline(inner, _limit):
                if inner.first: inner.first = False; return request
                import fcntl
                with (self.root / 'application.lock').open('rb') as held:
                    with self.assertRaises(BlockingIOError): fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(admission, '_lease_capability', None):
                    with self.assertRaisesRegex(ValueError, 'HANDOFF_UNCONFIRMED'): DirectoryLock(self.other)
                return b'STOP\n'
        with patch('sys.stdin', Input()), patch('sys.stdout', io.StringIO()): self.assertEqual(admission.lease(), 0)
        self.assertFalse((self.root / 'active').exists())
        with patch.object(admission, '_lease_capability', None), patch('sys.stdin', io.TextIOWrapper(io.BytesIO(request))), patch('sys.stdout', io.StringIO()):
            self.assertEqual(admission.lease(), 1)
        self.assertTrue((self.root / 'active').exists())
        with patch.object(admission, '_lease_capability', None):
            with self.assertRaisesRegex(ValueError, 'HANDOFF_UNCONFIRMED'): DirectoryLock(self.other)

    def test_replaced_or_permissive_application_marker_cannot_be_cleared(self):
        admission.begin(self.operation, self.capability)
        (self.root / 'pending').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'HANDOFF_UNCONFIRMED'): admission.finish(self.operation, self.capability)
        (self.root / 'pending').chmod(0o600)
        original = (self.root / 'pending').read_bytes(); (self.root / 'pending').unlink()
        external = Path(self.temp.name) / 'canary'; external.write_bytes(original); external.chmod(0o600)
        (self.root / 'pending').symlink_to(external)
        with self.assertRaises(OSError): admission.finish(self.operation, self.capability)
        self.assertEqual(external.read_bytes(), original)
