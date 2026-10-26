from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
from unittest.mock import patch
from loopcast.backup import create_backup
from loopcast.domain import Problem, uid
from loopcast.media import MediaLibrary
from loopcast.restore import restore_backup
from loopcast.schedule_config import ScheduleSettings
from loopcast.social import SocialAdapter
from loopcast.storage import Store
from loopcast.studio_service import StudioControl
from schedule_support import ScheduleFixture


class CountingSocial(SocialAdapter):
    def __init__(self, directory, outcome='published'):
        super().__init__(directory,{'backend':'contract','sending_enabled':True,'timeout':5,
            'contract_profiles':{'douyin':[{'stable_subject':'schedule-fixture','label':'Fixture'}]},
            'contract_results':{'schedule-fixture':outcome}})
        self.uploads=[];self.checks=[];self.entered=None;self.finish=None

    def _contract(self, operation, platform, work, payload, cancelled):
        if operation=='upload':
            self.uploads.append(payload['attempt_id'])
            if self.entered:
                self.entered.set()
                if not self.finish.wait(3):raise AssertionError('Upload barrier was not released')
        if operation=='check':self.checks.append(platform)
        return super()._contract(operation,platform,work,payload,cancelled)


class ScheduleExecutionTests(ScheduleFixture):
    def setUp(self):
        super().setUp();self.social=CountingSocial(self.store.directory)
        self.caps={p['platform']:p for p in self.social.capabilities()}
        account=self.store.studio_private_account(self.account['id'])
        (self.social.credentials/(account['credential_ref']+'.json')).write_text(json.dumps({'stable_subject':'schedule-fixture'}))
        self.control=StudioControl(self.store,MediaLibrary(self.store),self.social)

    def due_plan(self, groups=None, missed=None):
        spec=self.spec(groups,local_date='2026-10-26',local_time='12:01')
        if missed:spec['missed']=missed
        plan=self.create(spec);pre=self.preview(spec,plan)
        plan=self.store.schedule_control(plan['id'],'resume',plan['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.clock.value+=60
        return plan,spec

    def tick(self):return self.store.schedule_tick(self.caps,'contract',True)

    def drain(self, rounds=5):
        for _ in range(rounds):
            self.tick();job=self.store.studio_claim_job()
            if job:self.control.execute_job(job,threading.Event())

    def test_concurrent_due_ticks_restart_and_permanent_protection_call_once(self):
        plan,spec=self.due_plan()
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:self.tick(),range(8)))
        self.drain();self.assertEqual(len(self.social.uploads),1)
        reopened=Store(self.store.directory,clock=self.clock,schedule_settings=self.store.schedule_settings)
        reopened.studio_recover()
        for _ in range(5):reopened.schedule_tick(self.caps,'contract',True)
        self.assertEqual(len(self.social.uploads),1)
        report=reopened.schedule_occurrences(plan['id']);self.assertEqual(len(report['items']),1)
        self.assertEqual(report['items'][0]['targets'][0]['state'],'PUBLISHED')
        with self.assertRaises(Problem):self.create(spec)
        self.clock.value+=86400*365
        self.tick();self.assertEqual(len(self.social.uploads),1)

    def test_pause_wins_before_admission_and_explicit_resume_keeps_occurrence(self):
        plan,spec=self.due_plan();self.tick();job=self.store.studio_claim_job()
        paused=self.store.schedule_control(plan['id'],'pause',plan['control_version'],uid(),self.caps,'contract')
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM publication_quota_reservations').fetchone()[0],'RELEASED')
        pre=self.preview(spec,paused)
        self.store.schedule_control(plan['id'],'resume',paused['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.drain();self.assertEqual(len(self.social.uploads),1)
        self.assertEqual(len(self.store.schedule_occurrences(plan['id'])['items']),1)

    def test_pause_after_handoff_reports_inflight_and_retains_real_result(self):
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.social.entered,self.social.finish=threading.Event(),threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.control.execute_job,job,threading.Event())
            try:
                self.assertTrue(self.social.entered.wait(2))
                paused=self.store.schedule_control(plan['id'],'pause',plan['control_version'],uid(),self.caps,'contract')
                self.assertEqual(len(paused['in_flight']),1)
            finally:self.social.finish.set()
            future.result(3)
        self.assertEqual(len(self.social.uploads),1)
        self.assertEqual(self.store.schedule_get(plan['id'])['display_state'],'PUBLISHED')

    def _assert_unresolved(self, outcome):
        self.social.settings['contract_results']['schedule-fixture']=outcome
        plan,_=self.due_plan();self.drain();calls=len(self.social.uploads)
        self.assertEqual(calls,1)
        self.clock.value+=86400
        self.drain();self.assertEqual(len(self.social.uploads),calls)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM publication_quota_reservations ORDER BY rowid DESC LIMIT 1').fetchone()[0],'CONSUMED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM release_account_locks').fetchone()[0],1)
        self.assertEqual(self.store.schedule_get(plan['id'])['display_state'],'UNRESOLVED')

    def test_unknown_never_refunds_or_retries_across_new_days(self):
        self._assert_unresolved('lost_response')

    def test_submitted_never_refunds_or_retries_across_new_days(self):
        self._assert_unresolved('submitted')

    def test_sending_commit_crash_blocks_recovery_and_late_result(self):
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job();self.assertTrue(self.store.studio_sending(job))
        self.store.studio_recover();self.drain()
        self.assertEqual(self.social.uploads,[])
        self.assertEqual(self.store.schedule_get(plan['id'])['display_state'],'UNRESOLVED')
        self.assertFalse(self.store.studio_release_result(job,{'outcome':'PUBLISHED','confirmed':True,'work_id':'late'},'contract'))

    def test_midnight_migrates_reservation_and_consumes_only_current_budget(self):
        self.clock.value=datetime.fromisoformat('2026-10-26T23:58:59+00:00').timestamp()
        with self.store.transaction(True) as db:db.execute('UPDATE studio_accounts SET checked_at=?',(self.clock.iso(),))
        spec=self.spec(local_date='2026-10-26',local_time='23:59',missed={'policy':'BOUNDED_CATCHUP','max_age_seconds':3600,'max_slots':1})
        plan=self.create(spec);pre=self.preview(spec,plan)
        self.store.schedule_control(plan['id'],'resume',plan['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.clock.value+=1;self.tick();job=self.store.studio_claim_job()
        self.clock.value+=61;self.control.execute_job(job,threading.Event())
        self.assertEqual(len(self.social.uploads),1)
        with self.store.transaction() as db:
            row=db.execute('SELECT budget_day,state FROM publication_quota_reservations').fetchone()
            self.assertEqual(tuple(row),('2026-10-27','CONSUMED'))

    def test_clock_rollback_after_preparation_prevents_any_upload(self):
        self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.clock.value-=10
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM publication_quota_reservations').fetchone()[0],'RELEASED')

    def test_manual_and_scheduled_share_account_budget_without_replacing_targets(self):
        self.store.schedule_settings=ScheduleSettings(enabled=True,account_attempts_per_utc_day=1,plan_pending_targets=366,account_pending_targets=366,max_pending_targets=4096)
        first=self.group();target=self.store.studio_release(first['batch_id'])['targets'][0]
        self.store.studio_dispatch(first['batch_id'],target['id'],target['version'],uid(),self.caps,True)
        job=self.store.studio_claim_job();self.control.execute_job(job,threading.Event());self.assertEqual(len(self.social.uploads),1)
        plan,_=self.due_plan();self.drain()
        self.assertEqual(len(self.social.uploads),1)
        self.assertEqual(self.store.schedule_occurrences(plan['id'])['items'][0]['targets'][0]['state'],'WAITING_QUOTA')
        self.clock.value+=61;self.drain();self.assertEqual(len(self.social.uploads),1)

    def test_restore_old_plan_after_upload_never_performs_second_upload(self):
        plan,_=self.due_plan();package=self.root/'backup';report=create_backup(self.store.directory,package)
        self.drain();self.assertEqual(len(self.social.uploads),1)
        restored_path=self.root/'restored';restore_backup(package,report['manifest_sha256'],restored_path)
        restored=Store(restored_path,clock=self.clock,schedule_settings=self.store.schedule_settings)
        for _ in range(5):restored.schedule_tick(self.caps,'contract',True)
        self.assertEqual(len(self.social.uploads),1);self.assertEqual(restored.schedule_get(plan['id'])['control_state'],'PAUSED')
        self.assertTrue(restored.operations_status()['restore_guard'])

    def test_stale_account_checks_once_without_implicit_login_and_invalid_account_waits(self):
        plan,_=self.due_plan()
        with self.store.transaction(True) as db:db.execute("UPDATE studio_accounts SET checked_at='2026-10-25T12:00:00+00:00'")
        self.drain();self.assertEqual(len(self.social.checks),1);self.assertEqual(len(self.social.uploads),1)

    def test_queued_restore_guard_disabled_config_mode_and_changed_media_prevent_calls(self):
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.store.prepare_restored_state({'backup_id':uid(),'created_at':self.clock.iso(),'schema_version':8},'a'*64)
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])
        self.assertFalse(self.store.studio_sending(job))

    def test_queued_media_change_prevents_upload(self):
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job()
        batch=self.store.studio_release(self.store.schedule_get(plan['id'])['spec']['items'][0]['batch_id'])
        self.control.library.path(batch['asset_id']).write_bytes(b'changed')
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])

    def test_configuration_disabled_after_queue_prevents_upload(self):
        self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.store.schedule_settings=ScheduleSettings()
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])

    def test_mode_changed_after_queue_prevents_upload(self):
        self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.social.mode='live'
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])

    def test_capability_removed_after_queue_prevents_upload(self):
        self.due_plan();self.tick();job=self.store.studio_claim_job()
        original=self.social.capabilities()
        for cap in original:cap['video']=False
        with patch.object(self.social,'capabilities',return_value=original):self.control.execute_job(job,threading.Event())
        self.assertEqual(self.social.uploads,[])

    def test_bounded_catchup_persists_classification_across_restarts(self):
        # Thirty-five distinct dates exercise multiple bounded scan pages.
        self.store.schedule_settings=ScheduleSettings(enabled=True,max_pending_targets=4096,account_pending_targets=366,plan_pending_targets=366)
        groups=[self.group() for _ in range(35)]
        spec=self.spec(groups,kind='DAILY_LOCAL',start_local_date='2026-10-27',missed={'policy':'BOUNDED_CATCHUP','max_age_seconds':3600,'max_slots':1})
        del spec['local_date']
        plan=self.create(spec);pre=self.preview(spec,plan)
        self.store.schedule_control(plan['id'],'resume',plan['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.clock.value=datetime.fromisoformat('2026-11-30T09:10:00+00:00').timestamp()
        self.tick()
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM schedule_occurrences').fetchone()[0],32)
        self.store.studio_recover();self.drain(6)
        report=self.store.schedule_occurrences(plan['id'],limit=100)
        self.assertEqual(len(report['items']),35);self.assertEqual(len(self.social.uploads),1)
        for _ in range(3):self.store.studio_recover();self.drain()
        self.assertEqual(len(self.social.uploads),1)
        skipped=[item for item in report['items'] if item['reason']=='SKIPPED_MISSED']
        self.assertEqual(len(skipped),34)

    def test_default_missed_slots_skip_without_any_adapter_call(self):
        plan,_=self.due_plan();self.clock.value+=61;self.drain()
        self.assertEqual(self.social.uploads,[])
        self.assertEqual(self.store.schedule_occurrences(plan['id'])['items'][0]['reason'],'SKIPPED_MISSED')

    def test_cancel_before_admission_reuses_unsent_original_without_new_target(self):
        plan,spec=self.due_plan();self.tick();job=self.store.studio_claim_job()
        cancelled=self.store.schedule_control(plan['id'],'cancel',plan['control_version'],uid(),self.caps,'contract')
        self.control.execute_job(job,threading.Event());self.assertEqual(self.social.uploads,[])
        spec=spec|{'local_time':'12:02'};replacement=self.create(spec);pre=self.preview(spec,replacement)
        self.store.schedule_control(replacement['id'],'resume',replacement['control_version'],uid(),self.caps,'contract',pre['confirmation_digest'])
        self.clock.value+=60;self.drain();self.assertEqual(len(self.social.uploads),1)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM release_targets').fetchone()[0],1)
        self.assertEqual(cancelled['control_state'],'CANCELLED')

    def test_prepared_crash_releases_only_unadmitted_budget_and_needs_confirmation(self):
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job()
        self.store.studio_recover();self.drain()
        self.assertEqual(self.social.uploads,[])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM publication_quota_reservations').fetchone()[0],'RELEASED')
        self.assertEqual(self.store.schedule_occurrences(plan['id'])['items'][0]['targets'][0]['state'],'WAITING_CONFIRMATION')

    def test_legacy_same_day_attempt_counts_when_trusted_quota_is_first_enabled(self):
        self.store.schedule_settings=ScheduleSettings()
        group=self.group();target=self.store.studio_release(group['batch_id'])['targets'][0]
        self.store.studio_dispatch(group['batch_id'],target['id'],target['version'],uid(),self.caps,True)
        job=self.store.studio_claim_job();self.control.execute_job(job,threading.Event())
        self.assertEqual(len(self.social.uploads),1)
        self.store.schedule_settings=ScheduleSettings(enabled=True,account_attempts_per_utc_day=1,plan_pending_targets=366,account_pending_targets=366)
        plan,_=self.due_plan();self.drain();self.assertEqual(len(self.social.uploads),1)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM publication_quota_baselines').fetchone()[0],1)

    def test_expired_account_requires_explicit_recovery_without_implicit_login(self):
        plan,_=self.due_plan()
        with self.store.transaction(True) as db:db.execute("UPDATE studio_accounts SET checked_at='2026-10-25T12:00:00+00:00'")
        profiles=self.social.settings['contract_profiles']['douyin']
        self.social.settings['contract_profiles']['douyin']=[]
        self.drain();self.assertEqual(len(self.social.checks),1);self.assertEqual(self.social.uploads,[])
        self.assertEqual(self.store.studio_accounts()['items'][0]['state'],'EXPIRED')
        self.social.settings['contract_profiles']['douyin']=profiles
        self.drain();self.assertEqual(len(self.social.checks),1);self.assertEqual(self.social.uploads,[])
        account=self.store.studio_accounts()['items'][0]
        self.store.studio_check_account(account['id'],account['version'],uid())
        job=self.store.studio_claim_job();self.control.execute_job(job,threading.Event())
        self.drain();self.assertEqual(len(self.social.uploads),1)
        self.assertEqual(len(self.store.schedule_occurrences(plan['id'])['items']),1)

    def test_result_write_failure_after_upload_stays_unknown_without_another_call(self):
        import sqlite3
        plan,_=self.due_plan();self.tick();job=self.store.studio_claim_job()
        original=self.store.studio_release_result;failed=False
        def persist(value,result,source):
            nonlocal failed
            if not failed and result.get('outcome')=='PUBLISHED':
                failed=True;raise sqlite3.OperationalError('private-result-write-canary')
            return original(value,result,source)
        with patch.object(self.store,'studio_release_result',side_effect=persist):self.control.execute_job(job,threading.Event())
        self.assertTrue(failed);self.assertEqual(len(self.social.uploads),1)
        self.drain();self.assertEqual(len(self.social.uploads),1)
        state=self.store.schedule_get(plan['id']);self.assertEqual(state['display_state'],'UNRESOLVED')
        self.assertNotIn('private-result-write-canary',json.dumps(state))
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM publication_quota_reservations').fetchone()[0],'CONSUMED')

    def test_atomic_due_failure_rolls_back_slot_job_lock_and_reservation(self):
        plan,_=self.due_plan()
        with patch.object(self.store,'_schedule_event',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):self.tick()
        with self.store.transaction() as db:
            for table in ('schedule_occurrences','publication_quota_reservations','release_account_locks'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.drain();self.assertEqual(len(self.social.uploads),1)
