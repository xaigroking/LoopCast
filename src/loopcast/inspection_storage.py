"""Transactional inspection identities, single-flight checks and immutable reports."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from .domain import Problem, canonical, uid
from .inspection_models import ChannelReport, InspectionOutcome

ACTIVE = {'QUEUED','RUNNING'}


def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


class InspectionMethods:
    def _inspection_cursor(self, scope, after):
        raw=canonical({'scope':scope,'after':after}).encode()
        return base64.urlsafe_b64encode(raw+hmac.digest(self._schedule_cursor_key,raw,'sha256')).decode().rstrip('=')

    def _inspection_after(self, scope, cursor):
        if cursor is None:return None
        try:
            raw=base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4));body,signature=raw[:-32],raw[-32:]
            if not hmac.compare_digest(signature,hmac.digest(self._schedule_cursor_key,body,'sha256')):raise ValueError()
            value=json.loads(body)
            if set(value)!={'scope','after'} or value['scope']!=scope or not isinstance(value['after'],(str,int)):raise ValueError()
            return value['after']
        except (ValueError,TypeError,KeyError):raise Problem('INVALID_CURSOR','Reload the inspection list.',400) from None

    def _inspection_check(self, db, identity):
        row=db.execute('SELECT * FROM inspection_checks WHERE id=?',(identity,)).fetchone()
        if not row:raise Problem('HISTORY_RETIRED','This check is no longer retained.',410)
        return {k:row[k] for k in ('id','channel_id','channel_version','state','created_at','started_at','completed_at','report_id','reason_code')}|{'online':bool(row['online'])}

    def inspection_check(self, identity):
        with self.transaction() as db:return self._inspection_check(db,identity)

    def _inspection_release_account(self, db, check):
        if not check['online'] or not check['owns_job']:return
        private=json.loads(check['private_input']);previous=private.get('previous_account')
        if not previous:return
        db.execute("UPDATE studio_accounts SET state=?,checked_at=?,version=version+1 WHERE id=? AND version=? AND state='PENDING'",
                   (previous['state'],previous['checked_at'],previous['id'],private['expected_account_version']))

    def _inspection_descriptor(self, db, descriptor, *, strict=False):
        if descriptor['account_id']:
            actual=db.execute('SELECT version FROM studio_accounts WHERE id=?',(descriptor['account_id'],)).fetchone()
            if actual and actual[0]!=descriptor['scope']['account_version']:
                owned=db.execute("SELECT private_input FROM inspection_checks WHERE channel_id=? AND owns_job=1 AND state IN ('QUEUED','RUNNING')",(descriptor['id'],)).fetchone()
                private=json.loads(owned[0]) if owned else {}
                previous=private.get('previous_account',{})
                if private.get('expected_account_version')==actual[0] and previous.get('version')==descriptor['scope']['account_version']:
                    descriptor=descriptor|{'scope':descriptor['scope']|{'account_version':actual[0]}}
                elif strict:raise Problem('VERSION_CONFLICT','Refresh the account channel.',409)
        return descriptor

    def _inspection_sync(self, db, descriptor):
        descriptor=self._inspection_descriptor(db,descriptor,strict=True)
        row=db.execute('SELECT * FROM inspection_channels WHERE id=?',(descriptor['id'],)).fetchone()
        account_version=descriptor['scope']['account_version']
        if row is None:
            if db.execute('SELECT COUNT(*) FROM inspection_channels').fetchone()[0]>=256:raise Problem('CHANNEL_LIMIT','The inspection channel limit was reached.',429)
            db.execute('INSERT INTO inspection_channels(id,kind,account_id,version,input_digest,account_version,scope,inventory_at) VALUES(?,?,?,?,?,?,?,?)',
                       (descriptor['id'],descriptor['kind'],descriptor['account_id'],account_version or 1,descriptor['fingerprint'],account_version,canonical(descriptor['scope']),self.clock.iso()))
        elif row['input_digest']!=descriptor['fingerprint'] or row['account_version']!=account_version:
            active=db.execute("SELECT * FROM inspection_checks WHERE channel_id=? AND state IN ('QUEUED','RUNNING')",(row['id'],)).fetchone()
            if active:
                self._inspection_release_account(db,active)
                if active['owns_job']:db.execute("UPDATE studio_jobs SET state='CANCELLED' WHERE id=? AND token=? AND state IN ('READY','RUNNING')",(active['job_id'],active['token']))
                db.execute("UPDATE inspection_checks SET state='SUPERSEDED',reason_code='INPUT_CHANGED',completed_at=? WHERE id=?",(self.clock.iso(),active['id']))
                if descriptor['account_id']:
                    account_version=db.execute('SELECT version FROM studio_accounts WHERE id=?',(descriptor['account_id'],)).fetchone()[0]
            scope=descriptor['scope']|{'account_version':account_version}
            db.execute('UPDATE inspection_channels SET version=version+1,input_digest=?,account_version=?,scope=?,inventory_at=?,next_due=0 WHERE id=?',
                       (descriptor['fingerprint'],account_version,canonical(scope),self.clock.iso(),row['id']))
        else:db.execute('UPDATE inspection_channels SET inventory_at=? WHERE id=?',(self.clock.iso(),row['id']))
        return db.execute('SELECT * FROM inspection_channels WHERE id=?',(descriptor['id'],)).fetchone()

    def inspection_sync(self, descriptors):
        with self.admission_lock,self.transaction(True) as db:
            for descriptor in descriptors:self._inspection_sync(db,descriptor)

    def _inspection_report(self, db, identity, descriptor=None):
        row=db.execute('SELECT * FROM inspection_reports WHERE id=?',(identity,)).fetchone()
        if row is None:raise Problem('HISTORY_RETIRED','This report is no longer retained.',410)
        if descriptor:descriptor=self._inspection_descriptor(db,descriptor)
        value=json.loads(row['payload']);channel=db.execute('SELECT * FROM inspection_channels WHERE id=?',(row['channel_id'],)).fetchone()
        current=channel['version']
        if descriptor and (descriptor['fingerprint']!=channel['input_digest'] or descriptor['scope']['account_version']!=channel['account_version']):current+=1
        if current!=value['channel_version']:
            value.update(freshness='SUPERSEDED',superseded_by_version=current)
        elif datetime.fromtimestamp(self.clock.now(),timezone.utc)<datetime.fromisoformat(value['completed_at']) or datetime.fromtimestamp(self.clock.now(),timezone.utc)>=datetime.fromisoformat(value['valid_until']):
            value.update(freshness='EXPIRED',superseded_by_version=None)
        else:value.update(freshness='FRESH',superseded_by_version=None)
        return ChannelReport.model_validate(value).model_dump()

    def inspection_report(self, identity, descriptor=None):
        with self.transaction() as db:return self._inspection_report(db,identity,descriptor)

    def inspection_channel(self, descriptor, online_allowed=False):
        with self.transaction() as db:return self._inspection_channel(db,descriptor,online_allowed)

    def _inspection_channel(self, db, descriptor, online_allowed):
        descriptor=self._inspection_descriptor(db,descriptor)
        row=db.execute('SELECT * FROM inspection_channels WHERE id=?',(descriptor['id'],)).fetchone()
        version=(descriptor['scope']['account_version'] or 1) if not row else row['version']+int(row['input_digest']!=descriptor['fingerprint'] or row['account_version']!=descriptor['scope']['account_version'])
        active=db.execute("SELECT id FROM inspection_checks WHERE channel_id=? AND state IN ('QUEUED','RUNNING')",(descriptor['id'],)).fetchone()
        latest=self._inspection_report(db,row['latest_report'],descriptor) if row and row['latest_report'] else None
        cooldown=max(0,int(row['not_before']-self.clock.now()+.999)) if row else 0
        full=not row and db.execute('SELECT COUNT(*) FROM inspection_channels').fetchone()[0]>=256
        return {'id':descriptor['id'],'kind':descriptor['kind'],'channel_version':version,'scope':descriptor['scope'],
                'last_inventory_at':row['inventory_at'] if row else None,'latest_report':latest,'freshness':latest['freshness'] if latest else 'NOT_CHECKED',
                'active_check':self._inspection_check(db,active['id']) if active else None,
                'actions':{'local_check':not full and not cooldown,'online_check':online_allowed and not full and not cooldown,
                           'reason_code':'CHANNEL_LIMIT' if full else 'CHECK_COOLDOWN' if cooldown else 'OK','retry_after_seconds':cooldown}}

    def inspection_list(self, descriptors, allowed, cursor=None, limit=30):
        after=self._inspection_after('channels',cursor)
        values=sorted((d for d in descriptors if after is None or d['id']>after),key=lambda d:d['id'])
        with self.transaction() as db:
            return {'items':[self._inspection_channel(db,d,d['id'] in allowed) for d in values[:limit]],
                    'next_cursor':self._inspection_cursor('channels',values[limit-1]['id']) if len(values)>limit else None,
                    'policy':self.inspection_settings.model_dump()}

    def inspection_start(self, descriptor, expected_version, online, confirmed_online, key, *, account_check_allowed=False):
        request={'expected_version':expected_version,'online':online,'confirmed_online':confirmed_online}
        with self.admission_lock,self.transaction(True) as db:
            prior=self._studio_prior(db,'inspection:'+descriptor['id'],key,request)
            if prior:return {'check':self._inspection_check(db,prior['id']),'joined':False}
            channel=self._inspection_sync(db,descriptor)
            if channel['version']!=expected_version:raise Problem('VERSION_CONFLICT','Refresh the inspection channel.',409)
            if online and (descriptor['kind']!='ACCOUNT' or not confirmed_online or not account_check_allowed):raise Problem('CAPABILITY_UNAVAILABLE','This read-only online check is unavailable.',422)
            active=db.execute("SELECT id FROM inspection_checks WHERE channel_id=? AND state IN ('QUEUED','RUNNING')",(channel['id'],)).fetchone()
            if active:
                current=self._inspection_check(db,active['id'])
                if current['online']!=online:raise Problem('CHECK_BUSY','A different check is already active.',409)
                self._studio_remember(db,'inspection:'+channel['id'],key,request,{'id':current['id']})
                return {'check':current,'joined':True}
            if channel['not_before']>self.clock.now():raise Problem('CHECK_COOLDOWN','Wait for the inspection cooldown.',429)
            if db.execute("SELECT COUNT(*) FROM inspection_checks WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]>=self.inspection_settings.max_pending:raise Problem('QUEUE_FULL','The inspection queue is full.',429)
            identity=uid();private={};owns=True;state='QUEUED';job_id=None;token=uid();scope=descriptor['scope']
            deadline=self.clock.now()+self.inspection_settings.timeout_seconds
            if online:
                account=self._account(db,descriptor['account_id'])
                if db.execute('SELECT 1 FROM release_account_locks WHERE account_id=?',(account['id'],)).fetchone() or db.execute("SELECT 1 FROM auth_sessions WHERE account_id=? AND state IN ('PENDING','WAITING','VERIFYING') AND expires_at>?",(account['id'],self.clock.now())).fetchone():raise Problem('CHECK_BUSY','The account has active publication or authorization work.',409)
                existing=db.execute("SELECT * FROM studio_jobs WHERE kind='check' AND subject_id=? AND state IN ('READY','RUNNING') ORDER BY rowid LIMIT 1",(account['id'],)).fetchone()
                if existing:
                    context=json.loads(existing['context'])
                    if context.get('payload',{}).get('account_version')!=account['version']:raise Problem('CHECK_BUSY','The existing account check has another version.',409)
                    owns=False;token=existing['token'];job_id=existing['id'];state='RUNNING' if existing['state']=='RUNNING' else 'QUEUED'
                    private={'invoked':context.get('readonly_check_invoked',False)}
                else:
                    private={'previous_account':account,'expected_account_version':account['version']+1}
                    self._studio_check_account(db,account['id'],account['version'],identity)
                    existing=db.execute("SELECT * FROM studio_jobs WHERE kind='check' AND subject_id=? AND state='READY' ORDER BY rowid DESC LIMIT 1",(account['id'],)).fetchone()
                    token=existing['token'];job_id=existing['id'];db.execute('UPDATE studio_jobs SET deadline=? WHERE id=?',(deadline,job_id))
                    scope=scope|{'account_version':account['version']+1}
                    db.execute('UPDATE inspection_channels SET account_version=?,scope=? WHERE id=?',(scope['account_version'],canonical(scope),channel['id']))
            else:
                token=self._studio_enqueue(db,'inspection',identity,deadline,token=token)
                job_id=db.execute('SELECT id FROM studio_jobs WHERE token=?',(token,)).fetchone()[0]
            private['monotonic_deadline']=time.monotonic()+self.inspection_settings.timeout_seconds
            db.execute('INSERT INTO inspection_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity,channel['id'],channel['version'],state,int(online),self.clock.iso(),self.clock.iso() if state=='RUNNING' else None,None,deadline,token,self._execution_epoch(db),job_id,int(owns),canonical(scope),canonical(private),None,'CHECK_QUEUED'))
            self._studio_remember(db,'inspection:'+channel['id'],key,request,{'id':identity})
            return {'check':self._inspection_check(db,identity),'joined':not owns}

    def inspection_running(self, job):
        with self.transaction(True) as db:
            rows=db.execute("SELECT * FROM inspection_checks WHERE job_id=? AND token=? AND state IN ('QUEUED','RUNNING')",(job['id'],job['token'])).fetchall()
            for row in rows:
                db.execute("UPDATE inspection_checks SET state='RUNNING',started_at=COALESCE(started_at,?),reason_code='CHECK_RUNNING' WHERE id=?",(self.clock.iso(),row['id']))
            return bool(rows)

    def _inspection_finish(self, db, check, outcome, scope=None, terminal='COMPLETED'):
        if check['report_id']:return
        outcome=InspectionOutcome.model_validate(outcome).model_dump();channel=db.execute('SELECT * FROM inspection_channels WHERE id=?',(check['channel_id'],)).fetchone()
        current=channel['version']==check['channel_version'] and self._execution_epoch(db)==check['execution_epoch'] and check['state'] in ACTIVE
        if not current and terminal!='INTERRUPTED':terminal='SUPERSEDED'
        elif (self.clock.now()>check['deadline'] or time.monotonic()>json.loads(check['private_input']).get('monotonic_deadline',float('inf'))) and terminal=='COMPLETED':
            terminal='TIMED_OUT';outcome.update(result='UNKNOWN',reason_code='CHECK_TIMEOUT',evidence_status='unverified')
        if current and terminal!='TIMED_OUT' and datetime.fromtimestamp(self.clock.now(),timezone.utc)<datetime.fromisoformat(check['started_at'] or check['created_at']):
            outcome.update(result='UNKNOWN',reason_code='CHECK_CLOCK_CHANGED',evidence_status='unverified')
        identity=uid();completed=self.clock.iso();value=outcome|{'id':identity,'channel_id':check['channel_id'],'channel_version':check['channel_version'],'check_id':check['id'],
             'observed_at':completed,'started_at':check['started_at'] or check['created_at'],'completed_at':completed,'valid_until':stamp(self.clock.now()+self.inspection_settings.freshness_seconds),
             'scope':scope or json.loads(check['scope']),'freshness':'FRESH' if current else 'SUPERSEDED','superseded_by_version':None if current else channel['version']}
        value=ChannelReport.model_validate(value).model_dump()
        db.execute('INSERT INTO inspection_reports VALUES(?,?,?,?,?)',(identity,check['channel_id'],check['channel_version'],check['id'],canonical(value)))
        db.execute('UPDATE inspection_checks SET state=?,completed_at=?,report_id=?,reason_code=? WHERE id=?',(terminal,completed,identity,outcome['reason_code'],check['id']))
        if current:
            db.execute('UPDATE inspection_channels SET latest_report=? WHERE id=?',(identity,check['channel_id']))
            self._inspection_observed(db,channel,value)

    def _inspection_observed(self, db, channel, report):
        previous=None
        if channel['latest_report']:
            prior=db.execute('SELECT payload FROM inspection_reports WHERE id=?',(channel['latest_report'],)).fetchone()
            if prior:previous=json.loads(prior[0])
        bad={'MISCONFIGURED','FAILED','UNKNOWN'}
        signature=lambda item:(item['result'],item['reason_code'],item['evidence_scope'])
        same=previous and previous['channel_version']==report['channel_version']
        if not previous:kind='ANOMALY' if report['result'] in bad else 'INITIAL_OBSERVATION'
        elif not same:kind='ANOMALY' if report['result'] in bad else 'CONFIG_CHANGED'
        elif signature(previous)==signature(report):kind=None
        elif previous['result'] in bad and report['result']=='CALLABLE':kind='RECOVERY'
        elif report['result'] in bad and previous['result'] not in bad:kind='ANOMALY'
        else:kind='CHANGE'
        if kind:
            value={'channel_id':channel['id'],'channel_version':report['channel_version'],'kind':kind,
                   'old_result':previous['result'] if previous else None,'new_result':report['result'],'reason_code':report['reason_code'],
                   'previous_report_id':previous['id'] if previous else None,'current_report_id':report['id'],'created_at':report['completed_at']}
            cur=db.execute('INSERT INTO inspection_events(channel_id,channel_version,kind,previous_report,current_report,created_at,payload) VALUES(?,?,?,?,?,?,?)',
                           (channel['id'],report['channel_version'],kind,value['previous_report_id'],report['id'],report['completed_at'],'{}'))
            value['id']=cur.lastrowid
            db.execute('UPDATE inspection_events SET payload=? WHERE id=?',(canonical(value),cur.lastrowid))
        failures=min(channel['failure_count']+1,16) if report['result'] in bad else 0
        delay=min(self.inspection_settings.backoff_max_seconds,self.inspection_settings.backoff_initial_seconds*2**max(0,failures-1)) if failures else 0
        now=self.clock.now();not_before=max(channel['not_before'],now+delay) if failures else 0
        due=not_before if failures else now+self.inspection_settings.interval_seconds
        db.execute('UPDATE inspection_channels SET failure_count=?,not_before=?,next_due=? WHERE id=?',(failures,not_before,due,channel['id']))
        self._inspection_trim(db,channel['id'])

    def _inspection_trim(self, db, channel):
        db.execute('DELETE FROM inspection_events WHERE channel_id=? AND id NOT IN (SELECT id FROM inspection_events WHERE channel_id=? ORDER BY id DESC LIMIT ?)',
                   (channel,channel,self.inspection_settings.events_per_channel))
        latest=db.execute('SELECT latest_report FROM inspection_channels WHERE id=?',(channel,)).fetchone()[0]
        rows=db.execute('SELECT id FROM inspection_reports WHERE channel_id=? ORDER BY rowid DESC',(channel,)).fetchall()
        keep={r[0]for r in rows[:self.inspection_settings.reports_per_channel]};keep.add(latest)
        for row in rows:
            if row[0] in keep:continue
            # Retire dependent old events in the same transaction before their reports.
            db.execute('DELETE FROM inspection_events WHERE previous_report=? OR current_report=?',(row[0],row[0]))
            db.execute('DELETE FROM inspection_reports WHERE id=?',(row[0],))
        retired_jobs=[r[0]for r in db.execute("SELECT job_id FROM inspection_checks WHERE channel_id=? AND owns_job=1 AND state NOT IN ('QUEUED','RUNNING') AND id NOT IN (SELECT check_id FROM inspection_reports) AND id NOT IN (SELECT id FROM inspection_checks WHERE channel_id=? ORDER BY rowid DESC LIMIT ?)",
                                              (channel,channel,self.inspection_settings.reports_per_channel))]
        db.execute("DELETE FROM inspection_checks WHERE channel_id=? AND state NOT IN ('QUEUED','RUNNING') AND id NOT IN (SELECT check_id FROM inspection_reports) AND id NOT IN (SELECT id FROM inspection_checks WHERE channel_id=? ORDER BY rowid DESC LIMIT ?)",
                   (channel,channel,self.inspection_settings.reports_per_channel))
        for job in retired_jobs:
            db.execute("DELETE FROM studio_jobs WHERE id=? AND state IN ('DONE','CANCELLED') AND id NOT IN (SELECT job_id FROM inspection_checks WHERE job_id IS NOT NULL)",(job,))

    def inspection_reports(self, descriptor, cursor=None, limit=30):
        after=self._inspection_after('reports:'+descriptor['id'],cursor)
        with self.transaction() as db:
            rows=db.execute('SELECT rowid,id FROM inspection_reports WHERE channel_id=? AND rowid<COALESCE(?,9223372036854775807) ORDER BY rowid DESC LIMIT ?',
                            (descriptor['id'],after,limit+1)).fetchall()
            oldest=db.execute('SELECT payload FROM inspection_reports WHERE channel_id=? ORDER BY rowid LIMIT 1',(descriptor['id'],)).fetchone()
            return {'items':[self._inspection_report(db,r['id'],descriptor)for r in rows[:limit]],
                    'next_cursor':self._inspection_cursor('reports:'+descriptor['id'],rows[limit-1]['rowid']) if len(rows)>limit else None,
                    'retained_from':json.loads(oldest[0])['completed_at'] if oldest else None}

    def inspection_events(self, channel=None, cursor=None, limit=30):
        scope='events:'+(channel or 'all');after=self._inspection_after(scope,cursor)
        with self.transaction() as db:
            rows=db.execute('SELECT * FROM inspection_events WHERE (? IS NULL OR channel_id=?) AND id<COALESCE(?,9223372036854775807) ORDER BY id DESC LIMIT ?',
                            (channel,channel,after,limit+1)).fetchall()
            oldest=db.execute('SELECT created_at FROM inspection_events WHERE (? IS NULL OR channel_id=?) ORDER BY id LIMIT 1',(channel,channel)).fetchone()
            return {'items':[json.loads(row['payload'])for row in rows[:limit]],
                    'next_cursor':self._inspection_cursor(scope,rows[limit-1]['id']) if len(rows)>limit else None,'retained_from':oldest[0] if oldest else None}


    def inspection_finish(self, job, outcome, terminal='COMPLETED'):
        with self.transaction(True) as db:
            check=db.execute('SELECT * FROM inspection_checks WHERE job_id=? AND token=?',(job['id'],job['token'])).fetchone()
            if check:self._inspection_finish(db,check,outcome,terminal=terminal)
            db.execute("UPDATE studio_jobs SET state='DONE' WHERE id=? AND token=? AND state='RUNNING'",(job['id'],job['token']))

    def inspection_account_deadline(self, job):
        with self.transaction()as db:
            row=db.execute("SELECT * FROM inspection_checks WHERE job_id=? AND token=? AND owns_job=1 AND state IN ('QUEUED','RUNNING')",(job['id'],job['token'])).fetchone()
            if not row:return None
            remaining=max(0,min(self.inspection_settings.timeout_seconds,row['deadline']-self.clock.now()))
            return min(json.loads(row['private_input']).get('monotonic_deadline',float('inf')),time.monotonic()+remaining)

    def _inspection_owned_expired(self, db, job):
        row=db.execute("SELECT * FROM inspection_checks WHERE job_id=? AND token=? AND owns_job=1 AND state IN ('QUEUED','RUNNING')",(job['id'],job['token'])).fetchone()
        return bool(row and (self.clock.now()>=row['deadline'] or time.monotonic()>=json.loads(row['private_input']).get('monotonic_deadline',float('inf'))))

    def _inspection_account_result(self, db, job, observation, applied, scope_version=None):
        checks=db.execute('SELECT * FROM inspection_checks WHERE job_id=? AND token=?',(job['id'],job['token'])).fetchall()
        for check in checks:
            scope=json.loads(check['scope'])
            if applied and scope_version is not None:
                scope['account_version']=scope_version
                db.execute('UPDATE inspection_channels SET account_version=?,scope=? WHERE id=? AND version=?',(scope_version,canonical(scope),check['channel_id'],check['channel_version']))
            if not applied:observation=observation|{'result':'UNKNOWN','reason_code':'INPUT_CHANGED','evidence_status':'unverified'}
            self._inspection_finish(db,check,observation,scope,terminal='COMPLETED' if applied else 'SUPERSEDED')

    def _inspection_account_failed(self, db, job, reason):
        check=db.execute('SELECT * FROM inspection_checks WHERE job_id=? AND token=? AND owns_job=1',(job['id'],job['token'])).fetchone()
        if not check:return False
        self._inspection_release_account(db,check)
        scope=json.loads(check['scope']);account=db.execute('SELECT version FROM studio_accounts WHERE id=?',(scope['account_id'],)).fetchone()
        scope['account_version']=account[0]
        db.execute('UPDATE inspection_channels SET account_version=?,scope=? WHERE id=? AND version=?',(account[0],canonical(scope),check['channel_id'],check['channel_version']))
        timeout=reason in {'TIMEOUT','CHECK_TIMEOUT'} or self.clock.now()>=check['deadline'] or time.monotonic()>=json.loads(check['private_input']).get('monotonic_deadline',float('inf'))
        outcome={'result':'UNKNOWN','reason_code':'CHECK_TIMEOUT' if timeout else 'CHECK_INTERRUPTED' if reason=='INTERRUPTED' else 'CHECK_UNAVAILABLE','evidence_scope':'ACCOUNT_IDENTITY','checks':[],'actual_operations':{'check':int(bool(json.loads(check['private_input']).get('invoked')))}}
        self._inspection_finish(db,check,outcome,scope,terminal='TIMED_OUT' if timeout else 'INTERRUPTED' if reason=='INTERRUPTED' else 'COMPLETED')
        return True

    def _inspection_recover(self, db):
        for check in db.execute("SELECT * FROM inspection_checks WHERE state='RUNNING'").fetchall():
            self._inspection_release_account(db,check)
            outcome={'result':'UNKNOWN','reason_code':'CHECK_INTERRUPTED','evidence_scope':'ACCOUNT_IDENTITY' if check['online'] else 'LOCAL_RUNTIME','checks':[],'actual_operations':{}}
            self._inspection_finish(db,check,outcome,terminal='INTERRUPTED')
        for check in db.execute("SELECT * FROM inspection_checks WHERE state='QUEUED' AND (deadline<=? OR execution_epoch!=?)",(self.clock.now(),self._execution_epoch(db))).fetchall():
            self._inspection_release_account(db,check)
            restored=check['execution_epoch']!=self._execution_epoch(db)
            outcome={'result':'UNKNOWN','reason_code':'CHECK_INTERRUPTED' if restored else 'CHECK_TIMEOUT','evidence_scope':'ACCOUNT_IDENTITY' if check['online'] else 'LOCAL_RUNTIME','checks':[],'actual_operations':{}}
            self._inspection_finish(db,check,outcome,terminal='INTERRUPTED' if restored else 'TIMED_OUT')
