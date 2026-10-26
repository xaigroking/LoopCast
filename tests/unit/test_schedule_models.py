from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pydantic import ValidationError
from loopcast import migrations
from loopcast.backup import create_backup, verify_backup
from loopcast.domain import Problem, uid
from loopcast.operations_snapshot import snapshot
from loopcast.restore import restore_backup
from loopcast.schedule_models import SchedulePreviewRequest, ScheduleCreateRequest
from loopcast.schedule_time import expand_slots, zone_bytes
from loopcast.storage import Store
from schedule_support import ScheduleFixture


class ScheduleModelsTests(ScheduleFixture):
    def test_strict_models_reject_ambiguous_fields_and_false_confirmation(self):
        spec=self.spec()
        for change in ({'local_time':'24:00'},{'local_date':'2026-02-30'},{'start_local_date':'2026-10-28'},
                       {'quota':{'attempts_per_utc_day':True,'pending_targets':20}},
                       {'missed':{'policy':'SKIP','max_slots':1}}, {'items':[spec['items'][0],spec['items'][0]]}):
            with self.subTest(change=change),self.assertRaises(ValidationError):SchedulePreviewRequest.model_validate({'spec':spec|change})
        for confirmation in (1,'true',False,None):
            with self.subTest(confirmation=confirmation),self.assertRaises(ValidationError):
                ScheduleCreateRequest.model_validate({'spec':spec,'preview_digest':'a'*64,'confirmed':confirmation})
        with self.assertRaises(ValidationError):SchedulePreviewRequest.model_validate({'spec':spec,'schedule_id':uid()})

    def test_dst_fold_gap_half_hour_and_whole_day_are_actual_rules(self):
        cases=[('Asia/Shanghai','2026-10-27','09:00','2026-10-27T01:00:00+00:00'),
               ('America/New_York','2024-11-03','01:30','2024-11-03T05:30:00+00:00'),
               ('Australia/Lord_Howe','2024-04-07','01:45','2024-04-06T14:45:00+00:00')]
        base=self.spec()
        for zone,day,clock,expected in cases:
            slot=expand_slots(base|{'timezone':zone,'local_date':day,'local_time':clock})[2][0]
            self.assertEqual(slot['scheduled_at_utc'],expected);self.assertEqual(slot['selected_fold'],0)
        for zone,day,clock in [('America/New_York','2024-03-10','02:30'),('Australia/Lord_Howe','2024-10-06','02:15'),('Pacific/Apia','2011-12-30','12:00')]:
            with self.subTest(zone=zone):
                with self.assertRaises(Problem) as error:expand_slots(base|{'timezone':zone,'local_date':day,'local_time':clock})
                self.assertEqual(error.exception.code,'NONEXISTENT_LOCAL_TIME')
                daily={k:v for k,v in base.items() if k!='local_date'}|{'kind':'DAILY_LOCAL','start_local_date':day,'timezone':zone,'local_time':clock}
                slot=expand_slots(daily)[2][0];self.assertIsNone(slot['scheduled_at_utc']);self.assertEqual(slot['reason'],'SKIPPED_TIME_GAP')
        for invalid in ('../UTC','/etc/passwd','Unknown/Place','UTC/../../passwd'):
            with self.assertRaises(Problem):zone_bytes(invalid)

    def test_pinned_tzif_and_slot_identity_survive_time_edit_and_host_change(self):
        spec=self.spec();raw=zone_bytes('UTC');first=expand_slots(spec,raw)[2][0]
        with patch('loopcast.schedule_time.zone_bytes',side_effect=AssertionError('Must use frozen bytes')):
            second=expand_slots(spec|{'local_time':'10:30'},raw)[2][0]
        self.assertEqual(first['slot_key'],second['slot_key']);self.assertNotEqual(first['scheduled_at_utc'],second['scheduled_at_utc'])
        self.assertEqual(first['tzif_sha256'],hashlib.sha256(raw).hexdigest())

    def test_same_target_concurrent_plan_creation_and_idempotent_restart(self):
        spec=self.spec();preview=self.preview(spec);key=uid()
        def create(_):
            try:return self.store.schedule_create(spec,preview['preview_digest'],uid(),self.caps,'contract')['id']
            except Problem as p:return p.code
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(create,range(8)))
        identities=[x for x in results if len(x)==32];self.assertEqual(len(identities),1)
        self.assertEqual(results.count('TARGET_ALREADY_SCHEDULED'),7)
        other=self.spec();pre=self.preview(other)
        plan=self.store.schedule_create(other,pre['preview_digest'],key,self.caps,'contract')
        reopened=Store(self.store.directory,clock=self.clock,schedule_settings=self.store.schedule_settings)
        self.assertEqual(reopened.schedule_create(other,pre['preview_digest'],key,self.caps,'contract')['id'],plan['id'])
        with reopened.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schedule_slots').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schedule_occurrences').fetchone()[0],0)
        self.assertEqual(plan['control_state'],'PAUSED')

    def test_expired_preview_account_change_pagination_and_input_identity(self):
        spec=self.spec();pre=self.preview(spec);self.clock.value+=61
        with self.assertRaises(Problem) as p:self.store.schedule_create(spec,pre['preview_digest'],uid(),self.caps,'contract')
        self.assertEqual(p.exception.code,'PREVIEW_EXPIRED')
        pre=self.preview(spec)
        with self.store.transaction(True) as db:db.execute('UPDATE studio_accounts SET version=version+1')
        with self.assertRaises(Problem) as p:self.store.schedule_create(spec,pre['preview_digest'],uid(),self.caps,'contract')
        self.assertEqual(p.exception.code,'VERSION_CONFLICT')
        groups=[self.group() for _ in range(33)]
        daily={k:v for k,v in spec.items() if k!='local_date'}|{'kind':'DAILY_LOCAL','start_local_date':'2026-10-27','items':groups}
        page=self.preview(daily);self.assertEqual(len(page['items']),31)
        next_page=self.store.schedule_preview(daily,self.caps,'contract',cursor=page['next_cursor'])
        self.assertEqual(len(next_page['items']),2);self.assertIsNone(next_page['next_cursor'])
        with self.assertRaises(Problem):self.store.schedule_preview(daily|{'local_time':'11:00'},self.caps,'contract',cursor=page['next_cursor'])

    def test_versions_controls_future_boundary_and_cancelled_target_reassignment(self):
        once=self.spec();groups=once['items']+[self.group()]
        spec={k:v for k,v in once.items() if k!='local_date'}|{'kind':'DAILY_LOCAL','start_local_date':'2026-10-27','items':groups}
        plan=self.create(spec);pre=self.preview(spec,plan)
        active=self.store.schedule_control(plan['id'],'resume',plan['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.assertEqual(active['control_state'],'ACTIVE')
        with self.assertRaises(Problem):self.store.schedule_control(plan['id'],'pause',1,uid(),self.caps,'contract')
        paused=self.store.schedule_control(plan['id'],'pause',active['control_version'],uid(),self.caps,'contract')
        edited=spec|{'local_time':'11:00'};pre=self.preview(edited,paused)
        updated=self.store.schedule_update(plan['id'],edited,paused['control_version'],pre['preview_digest'],'2026-10-28',uid(),self.caps,'contract')
        self.assertEqual(updated['spec_version'],2);self.assertEqual(updated['control_state'],'PAUSED')
        with self.store.transaction() as db:
            old=self.store._schedule_version(db,plan['id'],'2026-10-27');new=self.store._schedule_version(db,plan['id'],'2026-10-28')
            self.assertEqual(json.loads(old['spec'])['local_time'],'09:00');self.assertEqual(json.loads(new['spec'])['local_time'],'11:00')
        cancelled=self.store.schedule_control(plan['id'],'cancel',updated['control_version'],uid(),self.caps,'contract')
        with self.assertRaises(Problem):self.store.schedule_control(plan['id'],'resume',cancelled['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        replacement=self.create(once);self.assertNotEqual(replacement['id'],plan['id'])
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM release_targets').fetchone()[0],2)

    def test_v7_migration_rollback_readonly_compatibility_and_plan_backup(self):
        old=self.root/'v7';old.mkdir();path=old/'state.sqlite3'
        script=''.join(getattr(migrations,'VERSION_'+name) for name in ['ONE','TWO','THREE','FOUR','FIVE','SIX','SEVEN'])
        with sqlite3.connect(path) as db:
            db.executescript(script);db.execute("INSERT INTO studio_projects VALUES('retained','{}',3,'created','updated')")
        before=path.read_bytes();self.assertEqual(snapshot(old)['database']['schema']['value'],7);self.assertEqual(path.read_bytes(),before)
        package=self.root/'v7-backup';report=create_backup(old,package);self.assertEqual(verify_backup(package,report['manifest_sha256'])['schema_version'],7)
        with patch.object(migrations,'VERSION_EIGHT',migrations.VERSION_EIGHT+'; INVALID SQL'):
            with self.assertRaises(sqlite3.OperationalError):Store(old)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0],7)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schedules'").fetchone())
        upgraded=Store(old)
        with upgraded.transaction() as db:self.assertEqual(db.execute('SELECT version FROM studio_projects').fetchone()[0],3)
        spec=self.spec();plan=self.create(spec);pre=self.preview(spec,plan)
        self.store.schedule_control(plan['id'],'resume',plan['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        package=self.root/'plan-backup';report=create_backup(self.store.directory,package)
        target=self.root/'restored';restore_backup(package,report['manifest_sha256'],target);restored=Store(target)
        current=restored.schedule_get(plan['id']);self.assertEqual(current['control_state'],'PAUSED');self.assertEqual(current['pause_reason'],'RESTORE_GUARD')
        with restored.transaction() as db:
            row=db.execute('SELECT tzif,tzif_sha256 FROM schedule_versions').fetchone();self.assertEqual(hashlib.sha256(row['tzif']).hexdigest(),row['tzif_sha256'])
        self.assertFalse(restored.operations_status()['upload_allowed_by_recovery'])
