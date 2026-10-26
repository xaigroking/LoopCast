"""Read-only checks reuse the existing owner, diagnostics and account executor."""
import json
from pathlib import Path
import sys
import tempfile
from .backup import code_identity
from .domain import Problem
from .inspection_identity import descriptor
from .inspection_models import InspectionOutcome
from .runner import run, RunnerError


class InspectionService:
    def __init__(self, runtime):
        self.runtime=runtime;self.store=runtime.store
        try:self.application=code_identity()
        except Exception:self.application={}
        self.refresh(local_only=True)

    def descriptors(self):
        runtime=self.runtime
        with self.store.transaction()as db:epoch=self.store._execution_epoch(db)
        values=[descriptor(k,self.application,runtime.generator_settings,runtime.media_settings,runtime.social,execution_epoch=epoch) for k in ('CONTROL_STORAGE','GENERATOR','MEDIA')]
        for account in self.store.studio_accounts()['items']:
            private=self.store.studio_private_account(account['id'])
            values.append(descriptor('ACCOUNT',self.application,runtime.generator_settings,runtime.media_settings,runtime.social,private,execution_epoch=epoch))
        return values

    def find(self, identity):
        value=next((d for d in self.descriptors() if d['id']==identity),None)
        if not value:raise Problem('NOT_FOUND','The inspection channel does not exist.',404)
        return value

    def allowed(self, value):
        if value['kind']!='ACCOUNT':return False
        policy=self.store.inspection_settings;scope=value['scope'];social=self.runtime.social
        account=self.store.studio_private_account(scope['account_id'])
        if not account['credential_ref'] or account['state']=='DISCONNECTED':return False
        capability=next((p for p in social.capabilities() if p['platform']==scope['platform']),None)
        return bool(capability and capability['configured'] and scope['account_id'] in policy.online_accounts and
                    (scope['evidence_mode']=='contract' or policy.allow_live) and scope['evidence_mode']==social.mode)

    def refresh(self, local_only=False):
        values=self.descriptors()
        if local_only:values=[d for d in values if d['kind']!='ACCOUNT']
        else:
            with self.store.transaction() as db:known={r[0]for r in db.execute('SELECT id FROM inspection_channels')}
            values=[d for d in values if d['id'] in known]
        self.store.inspection_sync(values)

    def list(self, cursor=None, limit=30):
        values=self.descriptors()
        return self.store.inspection_list(values,{d['id']for d in values if self.allowed(d)},cursor,limit)

    def channel(self, identity):
        value=self.find(identity);return self.store.inspection_channel(value,self.allowed(value))

    def start(self, identity, body, key):
        value=self.find(identity)
        return self.store.inspection_start(value,body.expected_version,body.online,body.confirmed_online,key,account_check_allowed=self.allowed(value))

    def report(self, identity):
        with self.store.transaction() as db:row=db.execute('SELECT channel_id FROM inspection_reports WHERE id=?',(identity,)).fetchone()
        if not row:raise Problem('HISTORY_RETIRED','This report is no longer retained.',410)
        try:value=self.find(row[0])
        except Problem:value=None
        return self.store.inspection_report(identity,value)

    def account_invoked(self, job):
        with self.store.transaction(True) as db:
            row=db.execute('SELECT context FROM studio_jobs WHERE id=? AND token=?',(job['id'],job['token'])).fetchone()
            if row:
                value=json.loads(row[0]);value['readonly_check_invoked']=True
                db.execute('UPDATE studio_jobs SET context=? WHERE id=?',(json.dumps(value),job['id']))
            for check in db.execute('SELECT id,private_input FROM inspection_checks WHERE job_id=?',(job['id'],)).fetchall():
                value=json.loads(check['private_input']);value['invoked']=True
                db.execute('UPDATE inspection_checks SET private_input=? WHERE id=?',(json.dumps(value),check['id']))

    def before_account_result(self, job):self.refresh()

    def account_outcome(self, result, account=None):
        observation=result.get('observation',{})
        state=observation.get('result','CALLABLE' if result.get('valid')is True else 'UNKNOWN')
        reason=observation.get('reason_code','OK' if result.get('valid')is True else 'IDENTITY_UNVERIFIED')
        if state not in {'CALLABLE','FAILED','UNKNOWN'}:state='UNKNOWN'
        if reason not in {'OK','IDENTITY_INVALID','IDENTITY_UNVERIFIED','CHECK_TIMEOUT','CHECK_UNAVAILABLE'}:reason='CHECK_UNAVAILABLE'
        return {'result':state,'reason_code':reason,'evidence_status':('fixture_verified' if self.runtime.social.mode=='contract' else 'account_verified') if state=='CALLABLE' else 'unverified',
                'evidence_scope':'ACCOUNT_IDENTITY','checks':[{'check_type':'ACCOUNT_IDENTITY','source':'social.check','result':state,'reason_code':reason}],
                'declarations':self.declarations(account) if account else [],'actual_operations':{'check':1}}

    def declarations(self, account):
        cap=next(p for p in self.runtime.social.capabilities()if p['platform']==account['platform'])
        return [{'operation':operation,'supported':supported,'configured':cap['configured'],'native_verification':cap['verification'],
                 'evidence_status':'source_inspected' if supported else 'unsupported','evidence_scope':'CAPABILITY_DECLARATION'}
                for operation,supported in [('check',cap['configured']),('upload',cap['video']),('query',cap['query']),('native_schedule',cap['schedule'])]]

    def passive_account(self, value):
        account=self.store.studio_private_account(value['account_id']);social=self.runtime.social
        cap=next(p for p in social.capabilities()if p['platform']==account['platform'])
        declarations=self.declarations(account)
        disconnected=account['state']=='DISCONNECTED' or not account['credential_ref']
        return {'result':'NOT_CONFIGURED' if disconnected else 'UNVERIFIED','reason_code':'ACCOUNT_DISCONNECTED' if disconnected else 'ONLINE_CHECK_NOT_REQUESTED',
                'evidence_scope':'ACCOUNT_IDENTITY','checks':[{'check_type':'ACCOUNT_STATE','source':'account.state','result':'NOT_CONFIGURED' if disconnected else 'UNVERIFIED','reason_code':'STORED_OBSERVATION_ONLY','value':account['state']}],
                'declarations':declarations,'actual_operations':{}}

    def execute(self, job, stopping):
        if not self.store.inspection_running(job):self.store.studio_complete_job(job);return
        with self.store.transaction() as db:row=db.execute('SELECT channel_id FROM inspection_checks WHERE job_id=?',(job['id'],)).fetchone()
        terminal='COMPLETED'
        try:
            self.refresh();value=self.find(row[0])
            if not self.store.studio_job_active(job):raise RunnerError('CANCELLED')
            if value['kind']=='ACCOUNT':outcome=self.passive_account(value)
            else:
                with tempfile.TemporaryDirectory(prefix='inspection-',dir=self.store.directory) as temporary:
                    path=Path(temporary)/'input.json';path.touch(mode=0o600)
                    path.write_text(json.dumps({'kind':value['kind'],'directory':str(self.store.directory),'generator':self.runtime.generator_settings,'media':self.runtime.media_settings,'ready':self.runtime.ready()}))
                    raw=run([sys.executable,'-I','-m','loopcast.inspection_probe',str(path)],temporary,{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                            self.store.inspection_settings.timeout_seconds,lambda:stopping.is_set() or not self.store.studio_job_active(job),limit=65536)
                    outcome=InspectionOutcome.model_validate_json(raw).model_dump()
            self.refresh()
        except RunnerError as error:
            timeout=error.code=='TIMEOUT' or self.store.clock.now()>=job['deadline']
            terminal='TIMED_OUT' if timeout else 'INTERRUPTED' if stopping.is_set() else 'COMPLETED'
            outcome={'result':'UNKNOWN','reason_code':'CHECK_TIMEOUT' if timeout else 'CHECK_INTERRUPTED' if stopping.is_set() else 'CHECK_UNAVAILABLE','evidence_scope':'LOCAL_RUNTIME','checks':[],'actual_operations':{}}
        except Exception:
            outcome={'result':'UNKNOWN','reason_code':'CHECK_UNAVAILABLE','evidence_scope':'LOCAL_RUNTIME','checks':[],'actual_operations':{}}
        self.store.inspection_finish(job,outcome,terminal)
