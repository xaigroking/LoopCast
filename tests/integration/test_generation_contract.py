import os
from pathlib import Path
import tempfile
import unittest
from loopcast.generation import Generator
from loopcast.runner import RunnerError
from generation_support import settings


class GenerationTests(unittest.TestCase):
    def test_installed_cli_protocol_and_faults(self):
        with tempfile.TemporaryDirectory() as directory:
            config=settings(directory);generator=Generator(config)
            os.environ['PUBLISH_CANARY']='secret-value'
            try:
                path, metadata=generator.generate({'title':'small video'},Path(directory)/'work',lambda:False)
            finally: del os.environ['PUBLISH_CANARY']
            self.assertGreater(path.stat().st_size,0);self.assertEqual(metadata['width'],64)
            self.assertFalse((path.parents[3]/'config.toml').exists())
            for title,code in [('bad-json','INVALID_RESULT'),('overflow','OUTPUT_LIMIT'),('exit-failure','UPSTREAM_FAILED'),('broken-media','UPSTREAM_FAILED')]:
                with self.assertRaises(RunnerError) as error:
                    generator.generate({'title':title},Path(directory)/title,lambda:False)
                self.assertEqual(error.exception.code,code)
            (Path(directory)/'component/lib'/f'python{__import__("sys").version_info.major}.{__import__("sys").version_info.minor}'/'site-packages/app/launcher.py').write_text('drift')
            with self.assertRaises(RunnerError):generator.verify()

    def test_probe_paths_and_configuration_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config=settings(directory);generator=Generator(config);root=Path(directory)/'work';root.mkdir()
            outside=Path(directory)/'sample.mp4'
            for path in [outside,root/'missing.mp4']:
                with self.assertRaises(RunnerError):generator.inspect(str(path),root)
            (root/'link.mp4').symlink_to(outside)
            with self.assertRaises(RunnerError):generator.inspect('link.mp4',root)
            config['manifest']['artifact_sha256']='0'*64
            with self.assertRaises(ValueError):Generator(config)
