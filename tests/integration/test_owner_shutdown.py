from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from loopcast.backup import create_backup
from loopcast.ownership import DirectoryLock
from loopcast.runtime import ApplicationRuntime


class OwnerShutdownTests(unittest.TestCase):
    def test_incomplete_shutdown_keeps_lock_and_still_attempts_other_resource(self):
        for failed in ('worker','publisher'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);runtime=ApplicationRuntime(root/'data')
                try:
                    resource=getattr(runtime,failed)
                    with patch.object(resource,'close',side_effect=RuntimeError('private-shutdown-canary')):
                        with self.assertRaises(RuntimeError) as error:runtime.close()
                    self.assertNotIn('private-shutdown-canary',str(error.exception))
                    self.assertTrue(resource.thread.is_alive());self.assertFalse(runtime.ready())
                    other=runtime.publisher if failed=='worker' else runtime.worker
                    self.assertFalse(other.thread.is_alive())
                    with self.assertRaises(BlockingIOError):
                        with DirectoryLock(root/'data'):pass
                    with self.assertRaises(BlockingIOError):create_backup(root/'data',root/'unsafe-backup')
                    self.assertFalse((root/'unsafe-backup').exists())
                finally:runtime.close()
                with DirectoryLock(root/'data'):pass
