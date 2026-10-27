from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from loopcast.ownership import DirectoryLock
from loopcast.storage import Store
from loopcast.runtime_guard import NAME

class DesktopDiagnosticsCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='diagnostics private-canary ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('data','session','install','config','reports'):(self.root/name).mkdir(mode=0o700)
        (self.root/'install/.next').mkdir(mode=0o700);(self.root/'install/.next/BUILD_ID').write_text('a'*40)
        self.tool=self.root/'install/tool'
        self.tool.write_text('#!/bin/sh\ntouch "'+str(self.root/'EXECUTED')+'"\necho secret-executable-output\n');self.tool.chmod(0o700)
        resource={'path':str(self.tool),'sha256':hashlib.sha256(self.tool.read_bytes()).hexdigest()}
        self.config={'format':'loopcast.desktop.v1','mode':'development','locale':'en','gatewayPort':28788,'startTimeoutMs':30000,'stopTimeoutMs':6000,'hostRevision':'a'*40,'apiRevision':'a'*40,'webRevision':'a'*40,'nodeVersion':'24.22.1',
            'python':resource,'node':resource,'treeModule':resource,'dataDir':str(self.root/'data'),'sessionDir':str(self.root/'session'),'nextRoot':str(self.root/'install'),'configs':{},'credentials':'secret-manifest-canary'}
        self.manifest=self.root/'config/desktop.json';self.manifest.write_text(json.dumps(self.config));self.manifest.chmod(0o600)
    def command(self,*args):
        result=subprocess.run([sys.executable,'-I','-m','loopcast','desktop-diagnostics',*map(str,args)],capture_output=True,text=True,timeout=15,env={**os.environ,'SECRET_DIAGNOSTIC_ENV':'secret-environment-canary'})
        raw=result.stdout+result.stderr
        for secret in (str(self.root),'secret-executable-output','secret-manifest-canary','secret-environment-canary','secret-business-canary'):
            self.assertNotIn(secret,raw)
        self.assertFalse((self.root/'EXECUTED').exists())
        return result
    def test_installed_cli_no_manifest_missing_and_empty_directory_have_no_side_effects(self):
        result=self.command('--data-dir',self.root/'absent');self.assertEqual(result.returncode,0);self.assertFalse((self.root/'absent').exists())
        result=self.command('--manifest',self.manifest);self.assertEqual(result.returncode,0)
        checks={x['id']:x for x in json.loads(result.stdout)['checks']};self.assertEqual(checks['owner_lock']['code'],'LOCK_MISSING');self.assertEqual(list((self.root/'data').iterdir()),[])
        text=self.command('--manifest',self.manifest,'--format','text');self.assertIn('cannot confirm safe recovery',text.stdout)
    def test_installed_cli_active_owner_dynamic_wal_and_guard_remain_independent(self):
        store=Store(self.root/'data');store.create({'title':'secret-business-canary'},'fixture')
        store.prepare_restored_state({'backup_id':'private-backup-id','created_at':'2026-01-01T00:00:00Z','schema_version':9},'a'*64)
        marker=self.root/'data'/NAME;marker.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'1'*32}));marker.chmod(0o600)
        with DirectoryLock(self.root/'data',desktop_identity='1'*32),closing(store.connect()) as db:
            db.execute("UPDATE runs SET version=version+1")
            original={p.name:p.read_bytes() for p in (self.root/'data').iterdir() if p.is_file()}
            for _ in range(2):
                result=self.command('--manifest',self.manifest);self.assertEqual(result.returncode,0)
                checks={x['id']:x for x in json.loads(result.stdout)['checks']}
                self.assertEqual(checks['owner_lock']['value'],'HELD');self.assertEqual(checks['desktop_cleanup_guard']['value'],'PRESENT');self.assertEqual(checks['restore_publication_guard']['value'],'ACTIVE')
            self.assertEqual(original,{p.name:p.read_bytes() for p in (self.root/'data').iterdir() if p.is_file()})
    def test_continuous_writer_is_explicit_about_snapshot_scope_and_never_mutates_facts(self):
        store=Store(self.root/'data');store.create({'title':'secret-business-canary'},'fixture');stop=threading.Event();writes=[]
        def writer():
            with closing(store.connect()) as db:
                while not stop.is_set():
                    db.execute('UPDATE runs SET version=version+1');writes.append(True);time.sleep(.001)
        thread=threading.Thread(target=writer);thread.start()
        try:
            result=self.command('--manifest',self.manifest);report=json.loads(result.stdout)
            self.assertEqual(report['scope'],'NON_ATOMIC_OBSERVATIONS');self.assertTrue(writes)
            checks={x['id']:x for x in report['checks']}
            self.assertIn(checks['restore_publication_guard']['code'],{'OK','STATE_CHANGED','READ_STATE_UNAVAILABLE'})
        finally:stop.set();thread.join()
        with closing(store.connect()) as db:
            self.assertEqual(db.execute('SELECT version FROM runs').fetchone()[0],1+len(writes))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],1)
    def test_private_new_export_no_overwrite_protected_target_and_argument_errors_are_redacted(self):
        output=self.root/'reports/report.json';result=self.command('--manifest',self.manifest,'--output',output);self.assertEqual(result.returncode,0);self.assertEqual(output.stat().st_mode & 0o777,0o600)
        before=output.read_bytes();result=self.command('--manifest',self.manifest,'--output',output);self.assertEqual(result.returncode,1);self.assertEqual(output.read_bytes(),before)
        result=self.command('--manifest',self.manifest,'--output',self.root/'data/secret-business-canary');self.assertEqual(result.returncode,1);self.assertEqual(list((self.root/'data').iterdir()),[])
        for args in [('--format','secret-manifest-canary'),('--timeout-ms','secret-manifest-canary'),('--secret-manifest-canary',)]:self.assertEqual(self.command(*args).returncode,2)
        result=self.command('--manifest',self.manifest,'--max-bytes','1');self.assertEqual(result.returncode,1);self.assertIn('BUDGET_EXCEEDED',result.stdout)

    def test_actual_preflight_result_frames_distinguish_owner_guard_and_directory(self):
        def inspect(expected):
            result=subprocess.run([sys.executable,'-I','-m','loopcast.desktop_guard','validate-result',str(self.root/'data'),str(self.root/'session')],capture_output=True,text=True,timeout=10)
            self.assertLessEqual(len(result.stdout.encode()),512);self.assertEqual(result.stderr,'')
            self.assertEqual(json.loads(result.stdout),{'format':'loopcast.desktop-preflight.v1','code':expected})
            self.assertEqual(result.returncode,0 if expected=='OK' else 1)
            self.assertNotIn(str(self.root),result.stdout)
        inspect('OK')
        with DirectoryLock(self.root/'data'):inspect('OWNER_HELD')
        marker=self.root/'data'/NAME;marker.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'1'*32}));marker.chmod(0o600)
        inspect('DESKTOP_CLEANUP_UNCONFIRMED');marker.write_text('private-guard-canary');inspect('GUARD_UNSAFE');marker.unlink()
        (self.root/'session').chmod(0o755);inspect('DIRECTORY_UNSAFE');(self.root/'session').chmod(0o700)
        lock=self.root/'data/service.lock';linked=self.root/'reports/lock-link';os.link(lock,linked);inspect('FILE_UNSAFE');linked.unlink()

    def test_real_signal_cancellation_stops_collection_with_a_redacted_partial_report(self):
        large=self.root/'install/large-resource'
        with large.open('wb') as file:file.truncate(400*1024*1024)
        large.chmod(0o600)
        self.config['python']={'path':str(large),'sha256':'0'*64}
        self.manifest.write_text(json.dumps(self.config))
        process=subprocess.Popen([sys.executable,'-I','-m','loopcast','desktop-diagnostics','--manifest',str(self.manifest),'--max-bytes',str(512*1024*1024),'--timeout-ms','10000'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        observed=False;deadline=time.monotonic()+5
        try:
            while process.poll() is None and time.monotonic()<deadline:
                try:
                    for entry in Path(f'/proc/{process.pid}/fd').iterdir():
                        try:observed |= os.readlink(entry)==str(large)
                        except OSError:pass
                except FileNotFoundError:pass
                if observed:break
                time.sleep(.002)
            self.assertTrue(observed)
            process.terminate();stdout,stderr=process.communicate(timeout=5)
            self.assertEqual(process.returncode,1);self.assertEqual(stderr,'')
            report=json.loads(stdout);self.assertIn('CANCELLED',{x['code'] for x in report['checks']})
            checks={x['id']:x for x in report['checks']};self.assertEqual(checks['resource_node']['code'],'CANCELLED');self.assertEqual(checks['owner_lock']['code'],'CANCELLED')
            self.assertNotIn(str(self.root),stdout);self.assertEqual(list((self.root/'data').iterdir()),[])
        finally:
            if process.poll() is None:process.kill();process.wait()

    def test_export_uses_the_collection_budget_instead_of_restarting_its_limits(self):
        output=self.root/'reports/report.json'
        result=self.command('--manifest',self.manifest,'--max-bytes',len(self.manifest.read_bytes())+1,'--output',output)
        self.assertEqual(result.returncode,1);self.assertFalse(output.exists());self.assertEqual(list(output.parent.iterdir()),[])
