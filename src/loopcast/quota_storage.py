"""Shared publication attempt budgets, with durable conservative legacy evidence."""
from datetime import datetime, timezone
from .domain import Problem


class QuotaMethods:
    def publication_initialize_budget(self, target_id):
        if not self.schedule_settings.enabled:return
        with self.transaction(True) as db:
            target=db.execute('SELECT * FROM release_targets WHERE id=?',(target_id,)).fetchone()
            if target:
                _,day=self._publication_clock(db)
                self._quota_baseline(db,day,'',target)

    def _publication_clock(self, db):
        now = self.clock.now()
        high = db.execute('SELECT high_water FROM scheduling_state WHERE id=1').fetchone()[0]
        if now < high - 5:raise Problem('CLOCK_BACKWARD', 'The publication clock moved backwards. New admission is waiting.', 409)
        day = datetime.fromtimestamp(max(high, now), timezone.utc).date().isoformat()
        db.execute('UPDATE scheduling_state SET high_water=? WHERE id=1', (max(high, now),))
        return now, day

    def _quota_baseline(self, db, day, exclude, target):
        rows = db.execute('''SELECT a.*,t.platform,t.stable_subject,t.evidence_mode FROM release_attempts a
            JOIN release_targets t ON t.id=a.target_id LEFT JOIN publication_quota_reservations q ON q.attempt_id=a.id
            WHERE q.attempt_id IS NULL AND a.id!=? AND t.platform=? AND t.stable_subject=? AND t.evidence_mode=? AND (substr(a.started_at,1,10)=? OR substr(a.ended_at,1,10)=?
                OR a.state IN ('SENDING','UNKNOWN','SUBMITTED') OR datetime(a.started_at) IS NULL OR (a.ended_at IS NOT NULL AND datetime(a.ended_at) IS NULL))''', (exclude,target['platform'],target['stable_subject'],target['evidence_mode'],day,day))
        for row in rows:
            try:
                if not row['started_at'] or datetime.fromisoformat(row['started_at']).tzinfo is None:raise ValueError()
                if row['ended_at'] and datetime.fromisoformat(row['ended_at']).tzinfo is None:raise ValueError()
            except (TypeError, ValueError):raise Problem('QUOTA_HISTORY_UNAVAILABLE', 'Legacy execution time evidence is incomplete.', 409) from None
            db.execute('INSERT OR IGNORE INTO publication_quota_baselines VALUES(?,?,?,?,?,?)',
                       (row['platform'],row['stable_subject'],row['evidence_mode'],day,row['id'],'legacy_conservative'))

    def _quota_used(self, db, target, day, schedule=None):
        if schedule:
            return db.execute("SELECT COUNT(*) FROM publication_quota_reservations WHERE schedule_id=? AND budget_day=? AND state IN ('RESERVED','CONSUMED')", (schedule,day)).fetchone()[0]
        key=(target['platform'],target['stable_subject'],target['evidence_mode'],day)
        current=db.execute("SELECT COUNT(*) FROM publication_quota_reservations WHERE platform=? AND stable_subject=? AND evidence_mode=? AND budget_day=? AND state IN ('RESERVED','CONSUMED')",key).fetchone()[0]
        old=db.execute('''SELECT COUNT(*) FROM publication_quota_baselines b WHERE platform=? AND stable_subject=? AND evidence_mode=? AND budget_day=?
            AND NOT EXISTS(SELECT 1 FROM publication_quota_reservations q WHERE q.attempt_id=b.attempt_id)''',key).fetchone()[0]
        return current+old

    def _quota_reserve(self, db, target, attempt, schedule=None, plan_limit=None):
        if not self.schedule_settings.enabled:return
        _,day=self._publication_clock(db)
        self._quota_baseline(db,day,attempt,target)
        pending = '''SELECT t.id,t.platform,t.stable_subject,t.evidence_mode FROM release_targets t WHERE
            t.state IN ('PREPARED','SENDING','UNKNOWN','SUBMITTED') OR EXISTS(
                SELECT 1 FROM schedule_target_owners o WHERE o.target_id=t.id AND o.active=1)'''
        if db.execute('SELECT COUNT(*) FROM (' + pending + ')').fetchone()[0] > self.schedule_settings.max_pending_targets:
            raise Problem('GLOBAL_QUOTA_PENDING', 'The trusted pending target bound has been reached.', 409)
        if db.execute('SELECT COUNT(*) FROM (' + pending + ') WHERE platform=? AND stable_subject=? AND evidence_mode=?',
                      (target['platform'],target['stable_subject'],target['evidence_mode'])).fetchone()[0] > self.schedule_settings.account_pending_targets:
            raise Problem('ACCOUNT_QUOTA_PENDING', 'The account pending target bound has been reached.', 409)
        if self._quota_used(db,target,day)>=self.schedule_settings.account_attempts_per_utc_day:
            raise Problem('ACCOUNT_QUOTA_EXHAUSTED', 'The account UTC-day attempt budget is exhausted.',409)
        if schedule and self._quota_used(db,target,day,schedule)>=min(plan_limit,self.schedule_settings.plan_attempts_per_utc_day):
            raise Problem('PLAN_QUOTA_EXHAUSTED','The plan UTC-day attempt budget is exhausted.',409)
        db.execute('INSERT INTO publication_quota_reservations VALUES(?,?,?,?,?,?,\'RESERVED\',?,?) ON CONFLICT(attempt_id) DO UPDATE SET budget_day=excluded.budget_day,state=excluded.state,updated_at=excluded.updated_at',
                   (attempt,schedule,target['platform'],target['stable_subject'],target['evidence_mode'],day,self.clock.iso(),self.clock.iso()))

    def _quota_admit(self, db, target, job):
        if not self.schedule_settings.enabled:return
        _,day=self._publication_clock(db)
        row=db.execute('SELECT * FROM publication_quota_reservations WHERE attempt_id=?',(job['payload']['attempt_id'],)).fetchone()
        binding=db.execute('''SELECT b.schedule_id,v.spec FROM schedule_job_bindings b JOIN schedule_occurrences o ON o.id=b.occurrence_id
            JOIN schedule_versions v ON v.schedule_id=o.schedule_id AND v.number=o.spec_version WHERE b.job_id=?''',(job['id'],)).fetchone()
        import json
        plan_limit=json.loads(binding['spec'])['quota']['attempts_per_utc_day'] if binding else None
        if row and row['state']!='RESERVED':raise Problem('EXECUTOR_UNRESOLVED','This budget reservation is no longer current.',409)
        if row:
            # Re-admission evaluates the current day and current trusted policy,
            # excluding this reservation, in the same transaction.
            db.execute("UPDATE publication_quota_reservations SET state='RELEASED' WHERE attempt_id=?",(row['attempt_id'],))
        self._quota_reserve(db,target,job['payload']['attempt_id'],binding['schedule_id'] if binding else None,plan_limit)
        db.execute("UPDATE publication_quota_reservations SET state='CONSUMED',updated_at=? WHERE attempt_id=?",(self.clock.iso(),job['payload']['attempt_id']))

    def _quota_result(self, db, job, outcome):
        if outcome=='NOT_SENT':
            db.execute("UPDATE publication_quota_reservations SET state='RELEASED',updated_at=? WHERE attempt_id=? AND state='RESERVED'",(self.clock.iso(),job['payload']['attempt_id']))

    def _quota_recover(self, db):
        db.execute("UPDATE publication_quota_reservations SET state='RELEASED',updated_at=? WHERE state='RESERVED' AND attempt_id IN (SELECT id FROM release_attempts WHERE state IN ('NOT_SENT','CANCELLED'))",(self.clock.iso(),))
        db.execute("UPDATE publication_quota_reservations SET state='CONSUMED',updated_at=? WHERE state='RESERVED' AND attempt_id IN (SELECT id FROM release_attempts WHERE state IN ('SENDING','UNKNOWN','SUBMITTED','PUBLISHED'))",(self.clock.iso(),))
