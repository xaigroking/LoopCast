from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from loopcast.domain import uid
from loopcast.schedule_config import SystemClock
from loopcast.social import SocialAdapter
from loopcast.storage import Store


class Clock(SystemClock):
    def __init__(self, value='2026-10-26T12:00:00+00:00'):
        self.value = datetime.fromisoformat(value).timestamp()
    def now(self):return self.value


class ScheduleFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name);self.clock = Clock()
        self.store = Store(self.root/'data', clock=self.clock, schedule_settings={'enabled':True,'plan_pending_targets':366,'account_pending_targets':366,'max_pending_targets':4096})
        self.social = SocialAdapter(self.store.directory, {'backend':'contract','sending_enabled':True})
        self.caps = {p['platform']:p for p in self.social.capabilities()}
        self.store.studio_authorize('douyin',None,'qr','owner',uid());job=self.store.studio_claim_job()
        self.store.studio_authorization_result(job,{'stable_subject':'schedule-fixture','label':'Fixture'},uid(),'contract')
        self.account=self.store.studio_accounts()['items'][0]
        with self.store.transaction(True) as db:db.execute('UPDATE studio_accounts SET checked_at=?',(self.clock.iso(),))

    def group(self):
        identity=uid();raw=identity.encode();folder=self.store.directory/'library';folder.mkdir(exist_ok=True)
        (folder/(identity+'.mp4')).write_bytes(raw)
        asset=self.store.studio_register_asset({'id':identity,'name':'fixture','kind':'video','media_type':'video/mp4','size':len(raw),
            'sha256':hashlib.sha256(raw).hexdigest(),'storage_ref':identity+'.mp4','origin':'import','source':'unit fixture',
            'metadata':{'duration':1,'width':64,'height':64,'codec':'vp9'},'fingerprint':None},uid())[0]
        value={'asset_id':asset['id'],'title':'Fixture','description':'','cover_id':None,'targets':[{'account_id':self.account['id'],'fields':{'category':None,'visibility':'public'}}]}
        # Preflight freshness uses the actual runtime clock in the older release port.
        from unittest.mock import patch
        with patch('loopcast.release_storage.datetime') as dt:
            dt.now.return_value=datetime.fromtimestamp(self.clock.now(),timezone.utc);dt.fromisoformat.side_effect=datetime.fromisoformat
            check=self.store.studio_preflight(value,self.caps,'contract')
        batch=self.store.studio_create_release(value,check['digest'],uid(),self.caps,'contract')
        return {'batch_id':batch['id'],'target_ids':[batch['targets'][0]['id']]}

    def spec(self, groups=None, **changes):
        return {'kind':'ONCE','timezone':'UTC','local_date':'2026-10-27','local_time':'09:00','items':groups or [self.group()],
                'missed':{'policy':'SKIP'},'quota':{'attempts_per_utc_day':10,'pending_targets':366},**changes}

    def preview(self, spec, plan=None):
        return self.store.schedule_preview(spec,self.caps,'contract',plan['id'] if plan else None,plan['control_version'] if plan else None)

    def create(self, spec):
        preview=self.preview(spec)
        return self.store.schedule_create(spec,preview['preview_digest'],uid(),self.caps,'contract')
