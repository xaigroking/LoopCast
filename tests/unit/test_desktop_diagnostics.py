from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from loopcast import desktop_diagnostics as d
from loopcast.ownership import DirectoryLock
from loopcast.storage import Store

class DesktopDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('data','session','install','config','reports'):(self.root/name).mkdir(mode=0o700)
        (self.root/'install/.next').mkdir(mode=0o700)
        (self.root/'install/.next/BUILD_ID').write_text('a'*40)
        self.tool=self.root/'install/tool';self.tool.write_bytes(b'not executed: secret-tool-canary');self.tool.chmod(0o600)
        digest=hashlib.sha256(self.tool.read_bytes()).hexdigest()
        self.config={'format':'loopcast.desktop.v1','mode':'development','hostRevision':'a'*40,'apiRevision':'a'*40,'webRevision':'a'*40,
            'nodeVersion':'24.22.1','python':{'path':str(self.tool),'sha256':digest},'node':{'path':str(self.tool),'sha256':digest},'treeModule':{'path':str(self.tool),'sha256':digest},
            'dataDir':str(self.root/'data'),'sessionDir':str(self.root/'session'),'nextRoot':str(self.root/'install'),'configs':{},
            'credential':'manifest-secret-canary'}
        self.manifest=self.root/'config/desktop.json';self.save()
    def save(self):self.manifest.write_text(json.dumps(self.config));self.manifest.chmod(0o600)
    def report(self,**kwargs):return d.collect(self.manifest,**kwargs)
    def checks(self,report=None):return {x['id']:x for x in (report or self.report())['checks']}
    def state(self):
        return {str(p.relative_to(self.root)):(p.read_bytes(),p.stat().st_mode & 0o777) for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
    def assert_safe(self,report):
        for format in ('json','text'):
            raw=d.render(report,format)
            for secret in (str(self.root),'manifest-secret-canary','secret-tool-canary','environment-secret-canary','business-secret-canary','identity-secret-canary'):
                self.assertNotIn(secret,raw)
        self.assertEqual(report['scope'],'NON_ATOMIC_OBSERVATIONS')

    def test_missing_empty_and_invalid_inputs_never_create_state_or_execute_tools(self):
        before=self.state()
        checks=self.checks();self.assertEqual(checks['owner_lock']['code'],'LOCK_MISSING');self.assertEqual(checks['restore_publication_guard']['code'],'DATABASE_MISSING')
        self.assertEqual(before,self.state());self.assertEqual(list((self.root/'data').iterdir()),[])
        missing=self.root/'missing';report=d.collect(self.root/'absent',missing)
        self.assertFalse(missing.exists());self.assertEqual(self.checks(report)['data_directory']['code'],'DIRECTORY_MISSING');self.assert_safe(report)
        self.manifest.write_text('{"format":"broken","token":"manifest-secret-canary"}')
        report=self.report();self.assertEqual(self.checks(report)['manifest']['code'],'MANIFEST_INVALID');self.assert_safe(report)
        self.manifest.write_text('{"format":1,"format":2}')
        self.assertEqual(self.checks()['manifest']['code'],'MANIFEST_INVALID')

    def test_distinct_resource_directory_platform_and_runtime_evidence(self):
        self.config['node']['sha256']='0'*64;self.config['python']['path']=str(self.root/'missing-resource');self.save()
        (self.root/'install/.next/BUILD_ID').write_text('b'*40);(self.root/'session').chmod(0o755)
        with patch('loopcast.desktop_diagnostics.supported',return_value=False),patch('loopcast.desktop_diagnostics.platform.machine',return_value='arm64'):report=self.report()
        checks=self.checks(report)
        for key,code in [('resource_node','RESOURCE_DIGEST_MISMATCH'),('resource_python','RESOURCE_MISSING'),('resource_next','VERSION_MISMATCH'),('session_directory','DIRECTORY_UNSAFE'),('platform_capabilities','PLATFORM_UNSUPPORTED')]:self.assertEqual(checks[key]['code'],code)
        self.assertEqual(checks['sandbox_runtime']['status'],'unverified');self.assert_safe(report)

    def test_two_independent_guards_all_four_combinations_and_unknown_count(self):
        store=Store(self.root/'data')
        with closing(sqlite3.connect(store.database)) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("INSERT INTO release_targets VALUES('target','batch','account','fixture','subject','family','{}','UNKNOWN',1,'fixture')");db.commit()
        marker=self.root/'data'/d.NAME
        for desktop in (False,True):
            for restore in (False,True):
                if desktop:marker.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'1'*32}));marker.chmod(0o600)
                elif marker.exists():marker.unlink()
                with closing(sqlite3.connect(store.database)) as db:
                    db.execute('DELETE FROM restore_guard')
                    if restore:db.execute("INSERT INTO restore_guard VALUES(1,'identity-secret-canary',1,'ACTIVE','EXTERNAL_HISTORY_UNRECONCILED')")
                    db.commit()
                before=self.state();report=self.report();checks=self.checks(report)
                self.assertEqual(checks['desktop_cleanup_guard']['value'],'PRESENT' if desktop else 'ABSENT')
                self.assertEqual(checks['restore_publication_guard']['value'],'ACTIVE' if restore else 'CLEAR')
                self.assertEqual(checks['unknown_publications']['value'],1)
                self.assertEqual(before,self.state());self.assert_safe(report)
                self.assertNotIn('safe_to_send',d.render(report))

    def test_existing_owner_and_active_wal_are_read_without_touching_source_sidecars(self):
        store=Store(self.root/'data');store.create({'title':'business-secret-canary'},'test')
        with closing(store.connect()) as db,DirectoryLock(store.directory):
            db.execute("UPDATE runs SET version=version+1")
            before=self.state();report=self.report();checks=self.checks(report)
            self.assertEqual(checks['owner_lock']['value'],'HELD');self.assertEqual(checks['restore_publication_guard']['value'],'CLEAR')
            self.assertEqual(before,self.state());self.assert_safe(report)
        self.assertEqual(self.checks()['owner_lock']['value'],'NOT_HELD')

    def test_corrupt_symlink_hardlink_and_swapped_guards_are_unknown(self):
        marker=self.root/'data'/d.NAME;outside=self.root/'outside';outside.write_text('identity-secret-canary')
        marker.symlink_to(outside)
        self.assertEqual(self.checks()['desktop_cleanup_guard']['code'],'GUARD_UNSAFE');self.assertEqual(outside.read_text(),'identity-secret-canary')
        marker.unlink();os.link(outside,marker)
        self.assertEqual(self.checks()['desktop_cleanup_guard']['code'],'GUARD_UNSAFE')
        marker.unlink();marker.write_text('{bad json');marker.chmod(0o600)
        self.assertEqual(self.checks()['desktop_cleanup_guard']['code'],'GUARD_UNSAFE')
        marker.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'1'*32}))
        original=d.read_guard
        def replace(fd):
            result=original(fd);marker.unlink();marker.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'2'*32}));marker.chmod(0o600);return result
        with patch('loopcast.desktop_diagnostics.read_guard',side_effect=replace):
            self.assertEqual(self.checks()['desktop_cleanup_guard']['code'],'STATE_CHANGED')

    def test_database_link_hot_journal_and_copy_mutation_do_not_change_business_bytes(self):
        store=Store(self.root/'data');original=store.database.read_bytes()
        journal=self.root/'data/state.sqlite3-journal';journal.write_text('business-secret-canary')
        self.assertEqual(self.checks()['restore_publication_guard']['code'],'READ_STATE_UNAVAILABLE');journal.unlink()
        outside=self.root/'outside.db';store.database.rename(outside);store.database.symlink_to(outside)
        self.assertEqual(self.checks()['restore_publication_guard']['status'],'unavailable');self.assertEqual(outside.read_bytes(),original)
        store.database.unlink();outside.rename(store.database)
        real=d.Budget.read;changed=False
        def mutate(budget,fd,size):
            nonlocal changed
            result=real(budget,fd,size)
            if result.startswith(b'SQLite format') and not changed:
                changed=True;os.utime(store.database,None)
            return result
        with patch.object(d.Budget,'read',mutate):self.assertEqual(self.checks()['restore_publication_guard']['code'],'STATE_CHANGED')
        self.assertEqual(store.database.read_bytes(),original)

    def test_budget_cancellation_and_permission_errors_remain_unavailable_without_raw_values(self):
        for options,code in [({'max_bytes':1},'BUDGET_EXCEEDED'),({'cancelled':lambda:True},'CANCELLED'),({'timeout_ms':0},'BUDGET_EXCEEDED')]:
            report=self.report(**options);self.assertIn(code,{x['code'] for x in report['checks']});self.assert_safe(report)
        with patch('loopcast.desktop_diagnostics.directory',side_effect=PermissionError('private-exception-canary')):
            report=self.report();self.assertIn('PERMISSION_DENIED',{x['code'] for x in report['checks']});self.assert_safe(report)
        with patch('loopcast.desktop_diagnostics.resource',side_effect=RuntimeError('private-exception-canary')):
            report=self.report();self.assertEqual(self.checks(report)['resource_node']['code'],'DIAGNOSTICS_UNAVAILABLE');self.assertNotIn('private-exception-canary',d.render(report))

    def test_white_list_serialization_and_new_private_output_protect_all_existing_targets(self):
        report=self.report();self.assert_safe(report)
        output=self.root/'reports/report.json';d.export_report(output,report,self.manifest,self.config)
        self.assertEqual(output.stat().st_mode & 0o777,0o600);self.assertEqual(json.loads(output.read_text()),report)
        original=output.read_bytes()
        for target in (output,self.manifest,self.root/'data/new.json',self.root/'session/new.json',self.root/'install/new.json'):
            with self.assertRaises((d.DiagnosticError,OSError)):d.export_report(target,report,self.manifest,self.config)
        self.assertEqual(output.read_bytes(),original)
        alias=self.root/'reports/link';alias.symlink_to(output)
        with self.assertRaises((d.DiagnosticError,OSError)):d.export_report(alias,report,self.manifest,self.config)
        hard=self.root/'reports/hard';os.link(output,hard)
        with self.assertRaises((d.DiagnosticError,OSError)):d.export_report(hard,report,self.manifest,self.config)
        report['checks'][0]['value']='manifest-secret-canary'
        with self.assertRaises(d.DiagnosticError):d.render(report)

    def test_export_publish_failure_and_parent_replacement_leave_no_completed_report(self):
        report=self.report();output=self.root/'reports/report.json'
        with patch('loopcast.desktop_diagnostics.publish_entry',side_effect=OSError('private-exception-canary')):
            with self.assertRaises(OSError):d.export_report(output,report,self.manifest,self.config)
        self.assertEqual(list(output.parent.iterdir()),[])
        original=d.publish_entry
        def sync_failure(fd,source,target):
            original(fd,source,target);raise OSError('private-sync-canary')
        with patch('loopcast.desktop_diagnostics.publish_entry',side_effect=sync_failure):
            with self.assertRaises(OSError):d.export_report(output,report,self.manifest,self.config)
        self.assertEqual(list(output.parent.iterdir()),[])
        def swap(fd,source,target):
            result=original(fd,source,target)
            output.parent.rename(self.root/'moved');output.parent.mkdir(mode=0o700)
            return result
        with patch('loopcast.desktop_diagnostics.publish_entry',side_effect=swap):
            with self.assertRaises(d.DiagnosticError):d.export_report(output,report,self.manifest,self.config)
        self.assertEqual(list((self.root/'moved').iterdir()),[]);self.assertEqual(list(output.parent.iterdir()),[])

    def test_source_directory_replacement_invalidates_all_dependent_observations(self):
        store=Store(self.root/'data');before=store.database.read_bytes();original=d.owner_probe
        def replace(fd):
            result=original(fd);(self.root/'data').rename(self.root/'original-data');(self.root/'data').mkdir(mode=0o700)
            (self.root/'data/external-canary').write_text('business-secret-canary');return result
        with patch('loopcast.desktop_diagnostics.owner_probe',side_effect=replace):report=self.report()
        checks=self.checks(report)
        for key in ('data_directory','owner_lock','desktop_cleanup_guard','restore_publication_guard','unknown_publications'):
            self.assertEqual(checks[key]['code'],'STATE_CHANGED');self.assertIsNone(checks[key]['value'])
        self.assertEqual((self.root/'original-data/state.sqlite3').read_bytes(),before)
        self.assertEqual((self.root/'data/external-canary').read_text(),'business-secret-canary');self.assert_safe(report)

    def test_future_schema_and_budget_expiring_after_publish_never_produce_success(self):
        store=Store(self.root/'data')
        with closing(sqlite3.connect(store.database)) as db:db.execute('UPDATE schema_version SET version=999');db.commit()
        before=self.state();report=self.report()
        self.assertEqual(self.checks(report)['restore_publication_guard']['code'],'SCHEMA_UNSUPPORTED');self.assertEqual(self.state(),before)
        budget=d.Budget();original=d.publish_entry;output=self.root/'reports/report.json'
        def late(fd,source,target):
            original(fd,source,target);budget.end=time.monotonic()-1
        with patch('loopcast.desktop_diagnostics.publish_entry',side_effect=late):
            with self.assertRaises(d.DiagnosticError):d.export_report(output,report,self.manifest,self.config,budget=budget)
        self.assertEqual(list(output.parent.iterdir()),[])
