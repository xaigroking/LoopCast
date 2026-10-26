"""Installed ASGI schedules use real HTTP, immutable releases and contract uploads."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from loopcast.schedule_config import ScheduleSettings, SystemClock
from asgi_support import ASGIThread
from studio_support import configurations
from test_studio_api import Owner


class ControlledClock(SystemClock):
    def __init__(self):self.value=time.time()
    def now(self):return self.value


class ScheduleAPITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        paths=configurations(self.root/'components');self.clock=ControlledClock()
        self.server=ASGIThread(self.root/'data',media_settings=json.loads(paths[1].read_text()),social_settings=json.loads(paths[2].read_text()),
            schedule_settings={'enabled':True},clock=self.clock).start();self.addCleanup(self.server.stop)
        self.owner=Owner(self.server)
        code,self.asset=self.owner.request('/v1/studio/assets/import?kind=video',raw=(self.root/'components/sample.mp4').read_bytes(),
            headers={'Content-Type':'application/octet-stream','X-File-Name':'clip.mp4','X-Source':'Owned%20contract%20media'})
        self.assertEqual(code,200,self.asset)

    def spec(self,platform='douyin'):
        accounts=self.owner.request('/v1/studio/accounts')[1]['items']
        if not any(a['platform']==platform for a in accounts):self.owner.authorize(platform)
        account=next(a for a in self.owner.request('/v1/studio/accounts')[1]['items'] if a['platform']==platform)
        value={'asset_id':self.asset['id'],'title':'Scheduled fixture','description':'','cover_id':None,
               'targets':[{'account_id':account['id'],'fields':{'category':None,'visibility':'public','declaration':None}}]}
        code,check=self.owner.request('/v1/studio/releases/preflight',value);self.assertEqual(code,200,check);self.assertTrue(check['allowed'],check)
        code,batch=self.owner.request('/v1/studio/releases',value|{'preflight_digest':check['digest'],'confirmed':True});self.assertEqual(code,200,batch)
        date=(datetime.fromtimestamp(self.clock.now(),timezone.utc).date()+timedelta(days=1)).isoformat()
        return {'kind':'ONCE','timezone':'UTC','local_date':date,'local_time':'09:00',
                'items':[{'batch_id':batch['id'],'target_ids':[batch['targets'][0]['id']]}],
                'missed':{'policy':'SKIP'},'quota':{'attempts_per_utc_day':10,'pending_targets':32}}

    def preview(self,spec,plan=None):
        body={'spec':spec}
        if plan:body|={'schedule_id':plan['id'],'expected_version':plan['control_version']}
        code,value=self.owner.request('/v1/studio/schedules/preview',body);self.assertEqual(code,200,value);return value

    def create(self,spec,key=None):
        preview=self.preview(spec)
        body={'spec':spec,'preview_digest':preview['preview_digest'],'confirmed':True}
        code,value=self.owner.request('/v1/studio/schedules',body,key);self.assertEqual(code,200,value);return value,body

    def test_strict_api_paused_creation_idempotency_controls_and_versioned_edit(self):
        spec=self.spec();plan,body=self.create(spec,'create-plan')
        path='/v1/studio/schedules/'+plan['id']
        self.assertEqual(plan['control_state'],'PAUSED');self.assertEqual(self.owner.request('/v1/studio/schedules',body,'create-plan')[1]['id'],plan['id'])
        changed=body|{'confirmed':False};self.assertEqual(self.owner.request('/v1/studio/schedules',changed,'create-plan')[0],400)
        code,cap=self.owner.request('/v1/studio/schedules/capabilities');self.assertEqual(code,200);self.assertFalse(cap['native_scheduling'])
        self.assertEqual(self.owner.request(path+'/occurrences')[1]['items'],[])
        unsupported=spec|{'timezone':'Unknown/Zone'};self.assertEqual(self.owner.request('/v1/studio/schedules/preview',{'spec':unsupported})[0],422)
        for invalid in (spec|{'local_time':'24:00'},spec|{'timezone':True},spec|{'extra':'secret-canary'},spec|{'quota':{'attempts_per_utc_day':True,'pending_targets':32}}):
            code,error=self.owner.request('/v1/studio/schedules/preview',{'spec':invalid});self.assertEqual(code,400,error);self.assertNotIn('secret-canary',json.dumps(error))
        edited=spec|{'local_time':'10:00'};preview=self.preview(edited,plan)
        update={'spec':edited,'preview_digest':preview['preview_digest'],'expected_version':plan['control_version'],'effective_from_local_date':None}
        code,updated=self.owner.request(path,update,'edit-plan','PUT');self.assertEqual(code,200,updated);self.assertEqual(updated['spec_version'],2)
        self.assertEqual(self.owner.request(path,update,'edit-plan','PUT')[1]['control_version'],updated['control_version'])
        self.assertEqual(self.owner.request(path,update,'stale-edit','PUT')[0],409)
        preview=self.preview(edited,updated);resume={'expected_version':updated['control_version'],'confirmation_digest':preview['confirmation_digest'],'confirmed':True}
        code,active=self.owner.request(path+'/resume',resume,'enable');self.assertEqual(code,200,active);self.assertEqual(active['control_state'],'ACTIVE')
        self.assertEqual(self.owner.request(path+'/pause',{'expected_version':1})[0],409)
        paused=self.owner.request(path+'/pause',{'expected_version':active['control_version']})[1]
        cancelled=self.owner.request(path+'/cancel',{'expected_version':paused['control_version']})[1]
        self.assertEqual(cancelled['control_state'],'CANCELLED')
        self.assertEqual(self.owner.request(path+'/resume',resume)[0],409)
        paths=self.server.app.openapi()['paths'];self.assertIn('/v1/studio/schedules/{schedule_id}/occurrences',paths)
        self.assertEqual(set(paths['/v1/studio/schedules/{schedule_id}/resume']) & {'get','post','delete'}, {'post'})

    def test_installed_owner_executes_original_target_once_and_history_keeps_identity(self):
        spec=self.spec();plan,_=self.create(spec);path='/v1/studio/schedules/'+plan['id']
        original=self.server.runtime.social._contract;uploads=[]
        def invoke(operation,*args,**kwargs):
            if operation=='upload':uploads.append(args[2]['attempt_id'])
            return original(operation,*args,**kwargs)
        preview=self.preview(spec,plan)
        with patch.object(self.server.runtime.social,'_contract',side_effect=invoke):
            code,active=self.owner.request(path+'/resume',{'expected_version':plan['control_version'],'confirmation_digest':preview['confirmation_digest'],'confirmed':True})
            self.assertEqual(code,200,active)
            self.clock.value=datetime.fromisoformat(spec['local_date']+'T09:00:00+00:00').timestamp()
            final=self.owner.wait(path,lambda p:p['display_state']=='PUBLISHED',timeout=12)
            self.assertEqual(len(uploads),1)
            history=self.owner.request(path+'/occurrences')[1]['items'];self.assertEqual(len(history),1)
            target=history[0]['targets'][0];self.assertEqual(target['target_id'],spec['items'][0]['target_ids'][0]);self.assertTrue(target['attempt_id'])
            batch=self.owner.request('/v1/studio/releases/'+target['batch_id'])[1]
            self.assertEqual(batch['targets'][0]['receipts'][0]['outcome'],'PUBLISHED')
            self.assertEqual(batch['targets'][0]['attempts'][0]['id'],target['attempt_id'])
            self.clock.value+=86400;time.sleep(1.2);self.assertEqual(len(uploads),1)
        self.assertEqual(final['counts'],{'PUBLISHED':1})

    def test_default_disabled_invalid_cursor_and_dedicated_target_ownership(self):
        spec=self.spec();plan,_=self.create(spec);second_spec=self.spec('youtube');second,_=self.create(second_spec)
        code,page=self.owner.request('/v1/studio/schedules?limit=1');self.assertEqual(code,200);self.assertTrue(page['next_cursor'])
        code,other=self.owner.request('/v1/studio/schedules?limit=1&cursor='+page['next_cursor']);self.assertEqual(code,200);self.assertNotEqual(other['items'][0]['id'],page['items'][0]['id'])
        self.assertEqual(self.owner.request('/v1/studio/schedules?cursor=bad')[0],400)
        self.assertEqual(self.owner.request('/v1/studio/schedules/'+plan['id']+'/occurrences?cursor='+page['next_cursor'])[0],400)
        self.assertEqual(self.owner.request('/v1/studio/schedules?limit=0')[0],400)
        batch=self.owner.request('/v1/studio/releases/'+spec['items'][0]['batch_id'])[1];target=batch['targets'][0]
        self.assertFalse(target['can_dispatch']);self.assertFalse(target['can_cancel'])
        self.assertEqual(self.owner.request('/v1/studio/releases/'+batch['id']+'/targets/'+target['id']+'/dispatch',{'expected_version':target['version']})[0],409)
        self.server.runtime.store.schedule_settings=ScheduleSettings()
        cap=self.owner.request('/v1/studio/schedules/capabilities')[1];self.assertFalse(cap['enabled']);self.assertFalse(cap['effective_sending_enabled'])
        preview=self.preview(spec,plan)
        code,error=self.owner.request('/v1/studio/schedules/'+plan['id']+'/resume',{'expected_version':plan['control_version'],'confirmation_digest':preview['confirmation_digest'],'confirmed':True})
        self.assertEqual(code,409,error);self.assertEqual(error['error']['code'],'LOCAL_SCHEDULING_DISABLED')
