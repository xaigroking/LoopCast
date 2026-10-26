"""Bounded due-slot classification and existing-publication preparation."""
from datetime import datetime
import json
from .domain import Problem, uid
from .schedule_time import expand_slots

WAITING = {'READY','WAITING_ACCOUNT','WAITING_CAPABILITY','WAITING_QUOTA','BLOCKED_RESTORE','WAITING_CLOCK'}


class ScheduleExecutionMethods:
    def _schedule_require_job(self, db, job):
        binding = db.execute('''SELECT b.*,s.control_state,o.eligible_until FROM schedule_job_bindings b
            JOIN schedules s ON s.id=b.schedule_id JOIN schedule_occurrences o ON o.id=b.occurrence_id WHERE b.job_id=?''',(job['id'],)).fetchone()
        if not binding:return
        target=db.execute('SELECT evidence_mode FROM release_targets WHERE id=?',(job['subject_id'],)).fetchone()
        if not self.schedule_settings.enabled or target['evidence_mode']=='live' and not self.schedule_settings.allow_live:
            raise Problem('LOCAL_SCHEDULING_DISABLED','Automatic local publication is disabled.',409)
        if binding['control_state']!='ACTIVE':raise Problem('SCHEDULE_PAUSED','The owning schedule is not active.',409)
        if binding['execution_epoch']!=self._execution_epoch(db):raise Problem('EXECUTOR_UNRESOLVED','The schedule execution identity changed.',409)
        if binding['eligible_until'] is None or self.clock.now()>binding['eligible_until']:
            raise Problem('SLOT_EXPIRED','The original slot execution window expired.',409)
        owner=db.execute('SELECT schedule_id,active FROM schedule_target_owners WHERE target_id=?',(job['subject_id'],)).fetchone()
        if not owner or not owner['active'] or owner['schedule_id']!=binding['schedule_id']:
            raise Problem('EXECUTOR_UNRESOLVED','The target authorization is no longer current.',409)

    def _schedule_result(self, db, target, outcome, attempt=None):
        rows=db.execute('''SELECT t.* FROM schedule_occurrence_targets t WHERE target_id=? AND attempt_id=?''',(target,attempt)).fetchall() if attempt else []
        for row in rows:
            state='WAITING_CONFIRMATION' if outcome=='NOT_SENT' else outcome
            if row['state']==state:continue
            db.execute('UPDATE schedule_occurrence_targets SET state=?,reason=? WHERE occurrence_id=? AND target_id=?',(state,'PROVEN_NOT_SENT' if outcome=='NOT_SENT' else outcome,row['occurrence_id'],target))
            occurrence=db.execute('SELECT schedule_id FROM schedule_occurrences WHERE id=?',(row['occurrence_id'],)).fetchone()
            self._schedule_event(db,occurrence['schedule_id'],'TARGET_RESULT',state,row['occurrence_id'])
            self._schedule_aggregate(db,row['occurrence_id'])
            if outcome=='PUBLISHED':db.execute('UPDATE schedule_target_owners SET active=0 WHERE target_id=? AND schedule_id=?',(target,occurrence['schedule_id']))

    def _schedule_aggregate(self, db, occurrence):
        values=[r[0] for r in db.execute('SELECT state FROM schedule_occurrence_targets WHERE occurrence_id=?',(occurrence,))]
        if not values:return
        if all(s=='PUBLISHED' for s in values):state='PUBLISHED'
        elif any(s in {'UNKNOWN','SUBMITTED'} for s in values):state='UNRESOLVED'
        elif any(s=='SENDING' for s in values):state='SENDING'
        elif any(s=='PREPARED' for s in values):state='PREPARED'
        elif any(s=='PUBLISHED' for s in values):state='PARTIAL'
        elif all(s.startswith('SKIPPED_') for s in values):state='SKIPPED'
        elif any(s in WAITING for s in values):state=next((key for key in ('BLOCKED_RESTORE','WAITING_CLOCK','WAITING_QUOTA','WAITING_ACCOUNT','WAITING_CAPABILITY') if key in values),'WAITING_CHECKS')
        else:state='WAITING_CONFIRMATION'
        db.execute('UPDATE schedule_occurrences SET state=?,version=version+1 WHERE id=? AND state!=?',(state,occurrence,state))

    def _schedule_sync(self, db):
        rows=db.execute('''SELECT o.target_id,o.attempt_id,t.state FROM schedule_occurrence_targets o JOIN release_targets t ON t.id=o.target_id
            WHERE o.attempt_id IS NOT NULL AND o.state IN ('PREPARED','SENDING','UNKNOWN','SUBMITTED')
            AND t.state IN ('SENDING','PUBLISHED','UNKNOWN','SUBMITTED','NOT_SENT','CANCELLED') AND t.state!=o.state LIMIT 32''').fetchall()
        for row in rows:self._schedule_result(db,row['target_id'],row['state'],row['attempt_id'])
        self._quota_recover(db)

    def _schedule_revoke(self, db, identity, reason):
        rows=db.execute('''SELECT a.*,j.id job_id,b.occurrence_id FROM schedule_job_bindings b JOIN studio_jobs j ON j.id=b.job_id
            JOIN release_attempts a ON a.token=j.token WHERE b.schedule_id=? AND a.state='PREPARED' ''',(identity,)).fetchall()
        for row in rows:
            db.execute("UPDATE release_attempts SET state='NOT_SENT',ended_at=? WHERE id=?",(self.clock.iso(),row['id']))
            db.execute("UPDATE release_targets SET state='NOT_SENT',version=version+1 WHERE id=?",(row['target_id'],))
            db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE id=?",(row['job_id'],))
            db.execute('DELETE FROM release_account_locks WHERE token=?',(row['token'],))
            self._quota_result(db,{'payload':{'attempt_id':row['id']}},'NOT_SENT')
            db.execute("UPDATE schedule_occurrence_targets SET state='WAITING_CONFIRMATION',reason=? WHERE occurrence_id=? AND target_id=?",(reason,row['occurrence_id'],row['target_id']))
            self._schedule_aggregate(db,row['occurrence_id'])

    def _schedule_materialize(self, db, plan, slot, version, reason, deadline):
        identity=uid();stamp=self.clock.iso()
        db.execute('INSERT INTO schedule_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (identity,plan['id'],slot['slot_key'],version['number'],slot['nominal_local'],slot['timezone'],slot['tzif_sha256'],
                    slot['selected_fold'],slot['scheduled_at_utc'],deadline,'WAITING',reason,1,stamp,stamp))
        for target in slot['group']['target_ids']:
            state=reason if reason.startswith('SKIPPED_') else 'READY'
            db.execute('INSERT INTO schedule_occurrence_targets VALUES(?,?,?,?,?,NULL,NULL)',(identity,target,slot['group']['batch_id'],state,reason))
            if state.startswith('SKIPPED_'):db.execute('UPDATE schedule_target_owners SET active=0 WHERE schedule_id=? AND target_id=?',(plan['id'],target))
        self._schedule_event(db,plan['id'],'CLASSIFIED',reason,identity)
        self._schedule_aggregate(db,identity)

    def schedule_tick(self, capabilities, mode, sending_enabled):
        if not self.schedule_settings.enabled:return 0
        with self.admission_lock,self.transaction(True) as db:
            self._schedule_sync(db)
            try:now,day=self._publication_clock(db)
            except Problem:
                db.execute("UPDATE schedules SET pause_reason='CLOCK_BACKWARD' WHERE control_state='ACTIVE'")
                return 0
            db.execute("UPDATE schedules SET pause_reason=NULL WHERE pause_reason='CLOCK_BACKWARD'")
            plans=db.execute("SELECT s.* FROM schedules s LEFT JOIN schedule_scan_windows w ON w.schedule_id=s.id WHERE control_state='ACTIVE' AND EXISTS(SELECT 1 FROM schedule_target_owners own WHERE own.schedule_id=s.id AND own.active=1) ORDER BY COALESCE(w.cutoff,0),s.rowid LIMIT 100").fetchall()
            classified=0
            for plan in plans:
                if classified>=32:break
                window=db.execute('SELECT * FROM schedule_scan_windows WHERE schedule_id=?',(plan['id'],)).fetchone()
                if window and not window['complete']:
                    cutoff,cursor,used=window['cutoff'],window['cursor'],window['catchup_used']
                else:cutoff,cursor,used=now,window['cursor'] if window else 0,0
                slots=db.execute('SELECT * FROM schedule_slots WHERE schedule_id=? AND position>=? ORDER BY position',(plan['id'],cursor)).fetchall()
                complete=1;versions={}
                for saved in slots:
                    if classified>=32:complete=0;break
                    version=self._schedule_version(db,plan['id'],saved['nominal_date'])
                    if version['number'] not in versions:
                        versions[version['number']]={s['slot_key']:s for s in expand_slots(json.loads(version['spec']),bytes(version['tzif']))[2]}
                    slot=versions[version['number']][saved['slot_key']]
                    if db.execute('SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND slot_key=?',(plan['id'],saved['slot_key'])).fetchone():
                        cursor=saved['position']+1;continue
                    scheduled=datetime.fromisoformat(slot['scheduled_at_utc']).timestamp() if slot['scheduled_at_utc'] else None
                    if scheduled is not None and scheduled>cutoff:break
                    spec=json.loads(version['spec']);policy=spec['missed'];reason='READY';deadline=None
                    if scheduled is None:reason='SKIPPED_TIME_GAP'
                    else:
                        age=cutoff-scheduled
                        deadline=scheduled+(policy['max_age_seconds'] if policy['policy']=='BOUNDED_CATCHUP' else 60)
                        if age>60:
                            if policy['policy']=='SKIP' or age>policy['max_age_seconds'] or used>=policy['max_slots']:reason='SKIPPED_MISSED'
                            else:used+=1
                    self._schedule_materialize(db,plan,slot,version,reason,deadline)
                    cursor=saved['position']+1;classified+=1
                db.execute('INSERT INTO schedule_scan_windows VALUES(?,?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET cutoff=excluded.cutoff,watermark=excluded.watermark,cursor=excluded.cursor,catchup_used=excluded.catchup_used,complete=excluded.complete',
                           (plan['id'],cutoff,now,cursor,used,complete))
            candidates=db.execute('''SELECT t.*,o.schedule_id,o.eligible_until,o.spec_version FROM schedule_occurrence_targets t
                JOIN schedule_occurrences o ON o.id=t.occurrence_id JOIN schedules s ON s.id=o.schedule_id
                WHERE s.control_state='ACTIVE' AND t.state IN ('READY','WAITING_ACCOUNT','WAITING_CAPABILITY','WAITING_QUOTA','BLOCKED_RESTORE','WAITING_CLOCK')
                ORDER BY o.rowid,t.rowid LIMIT 32''').fetchall()
            for pending in candidates:
                target=db.execute('SELECT * FROM release_targets WHERE id=?',(pending['target_id'],)).fetchone()
                if pending['eligible_until'] is None or now>pending['eligible_until']:
                    self._schedule_wait(db,pending,'SKIPPED_MISSED','SLOT_EXPIRED')
                    db.execute('UPDATE schedule_target_owners SET active=0 WHERE target_id=?',(target['id'],));continue
                try:self._quota_baseline(db,day,'',target)
                except Problem as error:
                    self._schedule_wait(db,pending,'WAITING_QUOTA',error.code)
                    continue
                db.execute('SAVEPOINT scheduled_target')
                try:
                    if target['evidence_mode']!=mode:raise Problem('EVIDENCE_MISMATCH','Publication evidence mode changed.',409)
                    if mode=='live' and not self.schedule_settings.allow_live:raise Problem('LOCAL_SCHEDULING_DISABLED','Live scheduling is disabled.',409)
                    version=db.execute('SELECT spec,frozen FROM schedule_versions WHERE schedule_id=? AND number=?',(pending['schedule_id'],pending['spec_version'])).fetchone()
                    spec=json.loads(version['spec']);frozen=next(t for t in json.loads(version['frozen']) if t['target_id']==target['id'])
                    asset=self._asset(db,frozen['asset_id'])
                    if asset['sha256']!=frozen['asset_sha256'] or target['stable_subject']!=frozen['stable_subject']:raise Problem('FROZEN_INPUT_CHANGED','The frozen publication identity changed.',409)
                    attempts=db.execute("SELECT COUNT(*) FROM release_attempts a LEFT JOIN publication_quota_reservations q ON q.attempt_id=a.id WHERE a.target_id=? AND (q.state='CONSUMED' OR a.state IN ('SENDING','UNKNOWN','SUBMITTED','PUBLISHED'))",(target['id'],)).fetchone()[0]
                    if attempts>=2:raise Problem('TARGET_ATTEMPT_LIMIT','The target attempt bound has been reached.',409)
                    job=self._prepare_dispatch(db,target,capabilities,sending_enabled,timeout=min(300,max(1,pending['eligible_until']-now)),schedule=pending['schedule_id'],plan_limit=spec['quota']['attempts_per_utc_day'])
                    db.execute('INSERT INTO schedule_job_bindings VALUES(?,?,?,?,?,?)',(job['job_id'],pending['occurrence_id'],target['id'],pending['schedule_id'],self._schedule_row(db,pending['schedule_id'])['version'],self._execution_epoch(db)))
                    db.execute("UPDATE schedule_occurrence_targets SET state='PREPARED',reason='QUEUED',attempt_id=?,job_id=? WHERE occurrence_id=? AND target_id=?",(job['attempt_id'],job['job_id'],pending['occurrence_id'],target['id']))
                    db.execute('RELEASE scheduled_target')
                    self._schedule_aggregate(db,pending['occurrence_id'])
                except Problem as error:
                    db.execute('ROLLBACK TO scheduled_target');db.execute('RELEASE scheduled_target')
                    reason=error.code
                    state='BLOCKED_RESTORE' if reason=='RESTORE_GUARD_ACTIVE' else 'WAITING_QUOTA' if 'QUOTA' in reason else 'WAITING_ACCOUNT' if reason.startswith('ACCOUNT_') else 'WAITING_CAPABILITY'
                    self._schedule_wait(db,pending,state,reason)
                    if reason=='ACCOUNT_STALE':self._schedule_account_check(db,pending,target)
            return classified

    def _schedule_wait(self, db, pending, state, reason):
        if pending['state']!=state or pending['reason']!=reason:
            db.execute('UPDATE schedule_occurrence_targets SET state=?,reason=? WHERE occurrence_id=? AND target_id=?',(state,reason,pending['occurrence_id'],pending['target_id']))
            self._schedule_event(db,pending['schedule_id'],'TARGET_WAIT',reason,pending['occurrence_id'])
            self._schedule_aggregate(db,pending['occurrence_id'])

    def _schedule_account_check(self, db, pending, target):
        kind='CHECK_'+target['account_id']
        if db.execute('SELECT 1 FROM schedule_events WHERE occurrence_id=? AND kind=?',(pending['occurrence_id'],kind)).fetchone():return
        account=self._account(db,target['account_id'])
        if account['state']!='AVAILABLE':return
        if db.execute("SELECT COUNT(*) FROM studio_jobs WHERE state IN ('READY','RUNNING')").fetchone()[0]>=32:return
        db.execute("UPDATE studio_accounts SET state='PENDING',version=version+1 WHERE id=?",(account['id'],))
        self._studio_enqueue(db,'check',account['id'],min(self.clock.now()+60,pending['eligible_until']),payload={'account_version':account['version']+1})
        self._schedule_event(db,pending['schedule_id'],kind,'ACCOUNT_STALE',pending['occurrence_id'])
