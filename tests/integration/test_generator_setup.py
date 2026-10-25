import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import io
import tarfile
import zipfile
import unittest
from unittest.mock import patch
from loopcast.generation import Generator, config_text
from loopcast.generator_diagnostics import inspect_generator
from loopcast.mpt_install import plan, install, checked_source, RESOURCES
from loopcast.contracts import MPT_REVISION
from generation_support import settings
from studio_support import configurations
from support import ServerProcess


class GeneratorSetupTests(unittest.TestCase):
    def test_non_generating_diagnostics_actual_cli_and_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=configurations(root/'components')
            generator=json.loads(paths[0].read_text());media=json.loads(paths[1].read_text())
            result=inspect_generator(generator,media)
            self.assertEqual(result['state'],'CALLABLE',result);self.assertEqual(result['live_verification'],'UNVERIFIED')
            self.assertEqual(result['paths']['online_model'],'UNVERIFIED')
            self.assertNotIn('contract-provider-canary',json.dumps(result));self.assertNotIn(directory,json.dumps(result))
            self.assertEqual(inspect_generator()['state'],'NOT_CONFIGURED')
            altered=generator|{'manifest':generator['manifest']|{'artifact_sha256':'0'*64}}
            self.assertEqual(inspect_generator(altered,media)['state'],'NOT_INSTALLED')
            Path(generator['config_ref']).chmod(0o644)
            self.assertEqual(inspect_generator(generator,media)['checks'][-1]['code'],'PROVIDER_CONFIG_INVALID')
            # Invalid optional generation configuration must leave the control plane usable.
            paths[0].write_text(json.dumps(altered))
            server=ServerProcess(root/'state',*paths).start()
            try:
                self.assertFalse(server.request('/v1/capabilities')[1]['video_generation'])
                code,report=server.request('/v1/studio/generator/diagnostics');self.assertEqual(code,200);self.assertFalse(report['callable'])
                self.assertEqual(server.request('/v1/runs')[1]['items'],[])
            finally:server.stop()

    def test_explicit_proxy_reaches_local_transport_and_edge_constructor_without_host_env(self):
        calls=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'local proxy response')
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config=settings(directory);config['network']={'proxy_url':'http://127.0.0.1:'+str(server.server_port),'allowed_proxy_hosts':['127.0.0.1']}
                with patch.dict(os.environ,{'PUBLISH_CANARY':'do-not-inherit'}):
                    generator=Generator(config);video,_=generator.generate({'title':'proxy-contract'},Path(directory)/'work',lambda:False)
                self.assertTrue(video.is_file());self.assertEqual(calls,['http://provider.invalid/contract'])
                config['network']['allowed_proxy_hosts']=['another.invalid']
                with self.assertRaises(ValueError):Generator(config)
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_install_plan_rejects_unpinned_source_and_validates_fresh_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(Exception):checked_source(root)
            source=root/'source';source.mkdir();uv=root/'uv';uv.write_text('operator tool');uv.chmod(0o700)
            executable=Path(os.__file__).parent.parent/'bin/python'
            with patch('loopcast.mpt_install.checked_source',return_value=(source,'/usr/bin/git')):
                value=plan(source,root/'destination',__import__('sys').executable,uv,hashlib.sha256(uv.read_bytes()).hexdigest(),uv,uv)
                self.assertEqual(value['revision'],MPT_REVISION);self.assertIn('frozen_runtime',value['steps'])
                with self.assertRaises(ValueError):plan(source,root,__import__('sys').executable,uv,'0'*64,uv,uv)
            pins=json.loads((RESOURCES/'mpt-source.json').read_text())
            self.assertEqual(pins['revision'],MPT_REVISION);self.assertIn('uv.lock',pins['files'])
            self.assertIn('--hash=sha256:',(RESOURCES/'mpt-build.lock').read_text())

    def test_provider_config_rejects_wrong_types_and_autopublish(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.toml';path.touch(mode=0o600)
            for text in ['[app]\nopenai_api_key=1','[app]\npexels_api_keys="key"','[app]\nupload_post_enabled=true','[app]\nunrecognized="x"']:
                path.write_text(text)
                with self.assertRaises(ValueError):config_text(path)

    def test_install_transaction_uses_frozen_upstream_lock_and_separate_build_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();(source/'uv.lock').write_text('contract lock')
            dest=root/'new-install';tool=root/'tool';tool.write_text('fixture');tool.chmod(0o700)
            value={'source':str(source),'destination':str(dest),'python':str(tool),'uv':str(tool),'uv_sha256':hashlib.sha256(tool.read_bytes()).hexdigest(),'git':'git','media':{'ffmpeg':str(tool),'ffprobe':str(tool),'ffmpeg_sha256':'a'*64,'ffprobe_sha256':'b'*64}}
            calls=[]
            def execute(args,cwd,env,**kwargs):
                calls.append(args)
                self.assertNotIn('PUBLISH_CANARY',env)
                if args[0]=='git':
                    with tarfile.open(dest/'source.tar','w') as bundle:
                        data=b'contract lock';info=tarfile.TarInfo('uv.lock');info.size=len(data);bundle.addfile(info,io.BytesIO(data))
                if 'from hatchling.build import build_wheel; import sys; build_wheel(sys.argv[1])' in args:
                    with zipfile.ZipFile(dest/'wheels/mpt-1.3.9-py3-none-any.whl','w') as bundle:
                        for name in ['app/cli.py','app/launcher.py','app/_assets/config.example.toml','app/_assets/resource/fonts/test.ttf']:bundle.writestr(name,b'contract only')
                if 'list' in args:return b'[{"name":"mpt","version":"1.3.9"}]'
                return b'contract tool'
            with patch('loopcast.mpt_install.run',side_effect=execute),patch.dict(os.environ,{'PUBLISH_CANARY':'private'}):record=install(value)
            self.assertEqual(record['state'],'INSTALLED');self.assertEqual(record['evidence'],'installation_only_live_generation_unverified')
            frozen=next(args for args in calls if 'sync' in args)
            self.assertIn('--frozen',frozen);self.assertIn('--no-install-project',frozen)
            self.assertTrue(any(str(dest/'build/bin/python') in args for args in calls))
            self.assertTrue(any(str(dest/'runtime/bin/python') in args for args in calls))
            config=json.loads((dest/'generator.json').read_text())
            self.assertEqual(config['manifest']['verification'],'unverified');self.assertEqual((dest/'generator.json').stat().st_mode&0o777,0o600)
