from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from loopcast import migrations
from loopcast.operations_snapshot import snapshot, render_snapshot, write_report
from loopcast.ownership import DirectoryLock
from loopcast.storage import Store


class OperationsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root / 'private-path-canary')
        self.store.create({'title':'input-secret-canary'}, 'fixture')

    def test_readonly_report_has_explicit_sources_unknowns_and_no_private_values(self):
        before = self.store.database.read_bytes(); events = self.store.list_runs()
        with patch.dict(os.environ, {'PRIVATE_SNAPSHOT_CANARY':'environment-secret-canary'}), DirectoryLock(self.store.directory):
            report = snapshot(self.store.directory, {'ffmpeg':str(self.root/'missing-secret-canary'),'api_key':'provider-secret-canary'},
                              {'manifest':{'revision':'a'*40,'artifact_sha256':'b'*64},'wheel':str(self.root/'private-wheel-canary'),'config_ref':'private-config-canary'})
        self.assertEqual(report['runtime']['owner_lock']['value'],'held')
        self.assertEqual(report['runtime']['service_ready']['status'],'unavailable')
        self.assertEqual(report['generator']['declared_revision']['status'],'unverified')
        self.assertEqual(report['generator']['wheel_sha256']['status'],'unavailable')
        self.assertEqual(report['media']['ffmpeg']['status'],'unavailable')
        self.assertEqual(report['database']['schema']['value'],9)
        self.assertEqual(self.store.database.read_bytes(),before); self.assertEqual(self.store.list_runs(),events)
        raw = json.dumps(report)
        for private in ('environment-secret-canary','input-secret-canary','provider-secret-canary','private-wheel-canary','private-config-canary',str(self.root)):
            self.assertNotIn(private,raw)
        self.assertEqual(set(report),{'format','observed_at','application','runtime','database','media','generator','recovery'})
        second = snapshot(self.store.directory); second['observed_at'] = report['observed_at']
        without_components = snapshot(self.store.directory); without_components['observed_at'] = report['observed_at']
        self.assertEqual(render_snapshot(second),render_snapshot(without_components))

    def test_tool_output_is_parsed_and_exceptions_never_become_observations(self):
        tool = self.root/'tool'; tool.write_bytes(b'unit executable marker')
        settings = {'ffmpeg':str(tool),'ffmpeg_sha256':hashlib.sha256(tool.read_bytes()).hexdigest()}
        with patch('loopcast.operations_snapshot.run', return_value=b'ffmpeg version 7.0.2-static build\nprivate-exception-canary /secret/path'):
            report = snapshot(self.store.directory,settings)
        self.assertEqual(report['media']['ffmpeg']['value'],'7.0.2-static'); self.assertNotIn('private-exception-canary',json.dumps(report))
        with patch('loopcast.operations_snapshot.run',side_effect=RuntimeError('secret-exception-canary')):
            self.assertEqual(snapshot(self.store.directory,settings)['media']['ffmpeg']['status'],'unavailable')

    def test_unknown_schema_rejected_and_v6_observation_does_not_migrate(self):
        old = self.root/'old';old.mkdir();path=old/'state.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript(''.join(getattr(migrations,k) for k in ('VERSION_ONE','VERSION_TWO','VERSION_THREE','VERSION_FOUR','VERSION_FIVE','VERSION_SIX')))
        original = path.read_bytes();report=snapshot(old)
        self.assertEqual(report['database']['schema']['value'],6);self.assertEqual(path.read_bytes(),original)
        self.assertEqual(report['recovery']['guard']['status'],'unavailable')
        with self.store.transaction(True) as db:db.execute('UPDATE schema_version SET version=999')
        with self.assertRaises(ValueError):snapshot(self.store.directory)

    def test_atomic_private_output_failure_and_protected_paths(self):
        report=snapshot(self.store.directory);output=self.root/'snapshot.json'
        write_report(output,render_snapshot(report),[self.store.directory]);self.assertEqual(output.stat().st_mode & 0o777,0o600)
        original=output.read_bytes()
        with patch('loopcast.operations_snapshot.os.replace',side_effect=OSError('controlled publish failure')):
            with self.assertRaises(OSError):write_report(output,'replacement',[self.store.directory])
        self.assertEqual(output.read_bytes(),original);self.assertEqual(list(self.root.glob('.loopcast-report-*')),[])
        with self.assertRaises(ValueError):write_report(self.store.database,'bad',[self.store.directory])
        alias=self.root/'alias';alias.symlink_to(output)
        with self.assertRaises(ValueError):write_report(alias,'bad')
        markdown=render_snapshot(report,'markdown');self.assertIn('| Field | Value | Status | Source |',markdown)
