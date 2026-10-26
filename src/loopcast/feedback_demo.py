"""Explicit fixture-only two-round workflow; fresh directory, no model or platform traffic."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import time
from .domain import uid
from .runtime import ApplicationRuntime
from .runner import run


def wait(read, condition):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        value=read()
        if condition(value):return value
        time.sleep(.04)
    raise RuntimeError('FIXTURE_DEADLINE')


def demonstrate(directory, media_settings):
    directory=Path(directory)
    if directory.exists():raise ValueError('A fresh fixture directory is required')
    settings={'backend':'contract','sending_enabled':True,'timeout':10,
              'contract_profiles':{'douyin':[{'stable_subject':'fixture-feedback-account','label':'Fixture feedback account'}]}}
    runtime=ApplicationRuntime(directory,media_settings=media_settings,social_settings=settings)
    owner='fixture-demo-owner';rounds=[]
    try:
        session=runtime.studio.authorize('douyin',None,'qr',owner,uid(),{})
        runtime.studio.interact(session['id'],owner,uid(),{'kind':'confirm','text':'fixture-feedback-account'})
        session=wait(lambda:runtime.store.studio_authorization(session['id'],owner),lambda s:s['state']=='SUCCEEDED')
        account=session['account_id'];decision=None
        for number,color,views in [(1,'orange',1200),(2,'blue',2000)]:
            if number==1:
                project=runtime.store.studio_create('Fixture round one',uid())
                project=runtime.store.studio_save(project['id'],project['draft']|{'script':'Explicit fixture narration, round one.'},project['version'],uid())
                project=runtime.store.studio_revision(project['id'],project['version'],uid())
            else:
                first=rounds[0]
                proposal={'snapshot_ids':[first['snapshot_id']],'topic':'Fixture round two','brief':'Review the selected observation before creating a new work.','goal':'views','minimum_age_hours':24,'minimum_views':100}
                decision=runtime.store.feedback_propose(proposal,uid())
                decision=runtime.store.feedback_confirm(decision['id'],{'expected_version':decision['version'],'topic':proposal['topic'],'brief':proposal['brief'],'project_id':None,'expected_project_version':None},uid())
                project=runtime.store.studio_project(decision['revisions'][-1]['project_id'])
            path=directory/('fixture-round-'+str(number)+'.mp4')
            run([media_settings['ffmpeg'],'-v','error','-f','lavfi','-i','color=c='+color+':s=64x64:d=0.4','-an','-c:v','libvpx-vp9',str(path)],directory,{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},15)
            asset=runtime.media.register(path,'video','Fixture round '+str(number),'Fixture color composition; no MPT or model invocation',uid());path.unlink()
            value={'asset_id':asset['id'],'title':'Fixture round '+str(number),'description':'Synthetic demonstration only','cover_id':None,
                   'targets':[{'account_id':account,'fields':{'category':None,'visibility':'public','declaration':None}}]}
            check=runtime.studio.preflight(value)
            batch=runtime.studio.create_release(value,check['digest'],uid());target=batch['targets'][0]
            runtime.store.studio_dispatch(batch['id'],target['id'],target['version'],uid(),runtime.studio.platform_map,True)
            batch=wait(lambda:runtime.store.studio_release(batch['id'],runtime.studio.query_platforms),lambda b:b['state']=='PUBLISHED')
            receipt=batch['targets'][0]['receipts'][-1]
            published=(datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
            binding=runtime.store.feedback_bind({'account_id':account,'asset_id':asset['id'],'work_id':receipt['work_id'],'release_target_id':target['id'],
                                                'published_at':published,'source':'Fixture synthetic publication age; not actual platform time'},uid())
            document={'format':'loopcast.metrics.v1','source':'Fixture round '+str(number),'evidence_mode':'contract','rows':[{
                'platform':'douyin','stable_subject':'fixture-feedback-account','work_id':binding['work_id'],'asset_id':asset['id'],
                'observed_at':datetime.now(timezone.utc).isoformat(),'aggregation':'cumulative','definition':'platform_reported_v1',
                'metrics':{'views':views,'likes':60 if number==1 else 80,'comments':5,'shares':10}}]}
            raw=json.dumps(document).encode();report=runtime.store.feedback_import(raw,'fixture-round-'+str(number)+'.json',uid())
            replay=runtime.store.feedback_import(raw,'same-file.json',uid())
            if replay['id']!=report['id'] or report['rows'][0]['state']!='ACCEPTED':raise RuntimeError('FIXTURE_IMPORT_FAILED')
            rounds.append({'project_id':project['id'],'revision_id':project['revisions'][0]['id'],'asset_id':asset['id'],'asset_sha256':asset['sha256'],
                           'release_id':batch['id'],'work_binding_id':binding['id'],'import_id':report['id'],'snapshot_id':report['rows'][0]['snapshot_id']})
        return {'evidence':'fixture_only_not_live_evidence','rounds':rounds,'decision':decision,
                'note':'Synthetic counters demonstrate traceability only; they do not establish a traffic improvement.'}
    finally:runtime.close()
