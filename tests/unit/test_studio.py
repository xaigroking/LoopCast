from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError
from loopcast.domain import Problem
from loopcast.storage import Store
from loopcast.studio_models import Draft, Scene
from loopcast import migrations


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_concurrent_create_replay_and_conflicts(self):
        with ThreadPoolExecutor(8) as pool:
            projects = list(pool.map(lambda _: self.store.studio_create('A project', 'one'), range(16)))
        self.assertEqual(len({p['id'] for p in projects}), 1)
        with self.assertRaises(Problem): self.store.studio_create('Different', 'one')
        self.assertEqual(len(self.store.studio_projects()['items']), 1)

    def test_draft_revisions_are_immutable_and_survive_restart(self):
        project = self.store.studio_create('Work', 'new')
        draft = project['draft'] | {'script': 'Original narration'}
        saved = self.store.studio_save(project['id'], draft, 1, 'save')
        frozen = self.store.studio_revision(project['id'], saved['version'], 'freeze')
        self.assertEqual(frozen, self.store.studio_revision(project['id'], saved['version'], 'freeze'))
        changed = self.store.studio_save(project['id'], draft | {'script': 'New narration'}, frozen['version'], 'edit')
        self.assertEqual(changed['revisions'][0]['draft']['script'], 'Original narration')
        self.assertEqual(Store(self.temp.name).studio_project(project['id']), changed)
        with self.assertRaises(Problem) as conflict:
            self.store.studio_save(project['id'], draft, 1, 'stale')
        self.assertEqual(conflict.exception.code, 'VERSION_CONFLICT')

    def test_strict_fields_nulls_and_scene_duration(self):
        for value in ({'title': None}, {'title': 'x', 'settings': None}, {'title': 'x', 'extra': True}):
            with self.assertRaises(ValidationError): Draft.model_validate(value)
        for duration in (True, '10', 0, 301):
            with self.assertRaises(ValidationError): Scene(id='a' * 32, narration='a', visual='', duration=duration)
        project = self.store.studio_create('Work', 'new')
        draft = project['draft'] | {'scenes': [{'id': 'a' * 32, 'narration': 'a', 'visual': '', 'duration': 10}] * 2}
        with self.assertRaises(Problem): self.store.studio_save(project['id'], draft, 1, 'bad-scenes')

    def test_failed_generation_does_not_leave_an_orphan_run(self):
        project = self.store.studio_create('Work', 'new')
        project = self.store.studio_revision(project['id'], project['version'], 'freeze')
        with self.assertRaises(Problem):
            self.store.studio_generate(project['id'], project['revisions'][0]['id'], 'script', project['version'], 'generate')
        self.assertEqual(self.store.list_runs()['items'], [])

    def test_v4_upgrade_preserves_old_runs_events_and_schema_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / 'state.sqlite3') as db:
                db.executescript(migrations.VERSION_ONE + migrations.VERSION_TWO + migrations.VERSION_THREE + migrations.VERSION_FOUR)
                db.execute("INSERT INTO runs VALUES('old','fixture','{}','digest','FAILED',1,'time','time',NULL)")
                db.execute("INSERT INTO events VALUES(1,'old',1,'old','time','{}')")
            migrated = Store(directory)
            with migrated.transaction() as db:
                self.assertEqual(db.execute('SELECT payload FROM events').fetchone()[0], '{}')
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], migrations.CURRENT_VERSION)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(migrated.snapshot('old')['state'], 'FAILED')
