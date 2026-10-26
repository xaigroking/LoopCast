"""Installed inspections: explicit requests, pending identity, safe scope and restore."""
import asyncio
import json
import os
from pathlib import Path
import uuid
from playwright.async_api import async_playwright, expect

BASE=os.environ['LOOPCAST_BASE_URL']
EMPTY=os.environ.get('LOOPCAST_EXPECT_EMPTY_INSPECTIONS')=='1'
RESTORED=os.environ.get('LOOPCAST_EXPECT_RESTORE_GUARD')=='1'


async def request(page,path,body=None,method=None):
    value=await page.evaluate('''async ({path,body,method,key})=>{const r=await fetch(path,{method:method||(body===null?'GET':'POST'),headers:body===null?{}:{'Content-Type':'application/json','Idempotency-Key':key},body:body===null?undefined:JSON.stringify(body)});return {status:r.status,value:await r.json()}}''',{'path':path,'body':body,'method':method,'key':uuid.uuid4().hex})
    assert value['status']in (200,202),(path,value)
    return value['value']


async def wait_report(page,channel_id):
    for _ in range(150):
        value=await request(page,'/v1/operations/channels/'+channel_id)
        if value['latest_report'] and not value['active_check']:return value['latest_report']
        await page.wait_for_timeout(100)
    raise AssertionError('No completed inspection report')


async def main():
    errors=[];external=[];forbidden=[]
    async with async_playwright()as tool:
        browser=await tool.chromium.launch(channel='chromium')
        context=await browser.new_context(locale='en-US',viewport={'width':1440,'height':1100});page=await context.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda r:external.append(r.url) if not r.url.startswith(BASE) else None)
        page.on('request',lambda r:forbidden.append(r.url) if r.method=='POST'and any(p in r.url for p in ('/generate','/authorizations','/dispatch','/publications')) else None)
        await page.goto(BASE+'/settings');await page.locator('#language-select').select_option('en')
        panel=page.locator('#channel-inspections');await expect(panel.get_by_role('heading',name='Channel inspections',exact=True)).to_be_visible()
        before=await request(page,'/v1/operations/snapshot');status=await request(page,'/v1/operations/status');inventory=await request(page,'/v1/operations/channels')
        assert inventory['policy']['enabled']is False
        if EMPTY:
            assert len(inventory['items'])==3 and not any(c['kind']=='ACCOUNT'for c in inventory['items'])
            card=panel.locator('[data-kind=GENERATOR]');await card.get_by_role('button',name='Run read-only check',exact=True).click()
            channel=next(c for c in inventory['items']if c['kind']=='GENERATOR');report=await wait_report(page,channel['id'])
            assert report['result']=='NOT_CONFIGURED'
            assert report['checks'][0]['result']=='NOT_CONFIGURED'
            await expect(card.get_by_text('No generator is configured.',exact=False)).to_be_visible(timeout=12000)
        elif RESTORED:
            assert status['restore_guard']and not status['upload_allowed_by_recovery']
            historical=[c for c in inventory['items']if c['latest_report']]
            assert historical and all(c['freshness']=='SUPERSEDED'for c in historical)
            control=next(c for c in inventory['items']if c['kind']=='CONTROL_STORAGE');card=panel.locator('[data-channel="'+control['id']+'"]')
            await card.get_by_role('button',name='Run read-only check',exact=True).click();report=await wait_report(page,control['id'])
            assert report['result']=='CALLABLE' and report['actual_operations']['upload']==0
            after=await request(page,'/v1/operations/status');assert after['restore_guard']==status['restore_guard']and not after['upload_allowed_by_recovery']
            assert not any(p['control_state']=='ACTIVE'for p in (await request(page,'/v1/studio/schedules'))['items'])
        else:
            media=next(c for c in inventory['items']if c['kind']=='MEDIA');path='/v1/operations/channels/'+media['id']+'/checks';writes=[];original={}
            async def lost(route):
                writes.append((route.request.headers.get('idempotency-key'),route.request.post_data_json))
                if not original:
                    response=await route.fetch();original.update(await response.json());await route.abort()
                else:await route.continue_()
            await page.route('**'+path,lost)
            card=panel.locator('[data-channel="'+media['id']+'"]');await card.get_by_role('button',name='Run read-only check',exact=True).click()
            await expect(panel.get_by_role('button',name='Retry the same check request',exact=True)).to_be_visible(timeout=12000)
            assert len(writes)==1
            await page.reload();await expect(page.locator('#channel-inspections')).to_be_visible();await page.wait_for_timeout(400)
            assert len(writes)==1,'Reload must not initiate a write'
            panel=page.locator('#channel-inspections')
            async with page.expect_response(lambda r:r.url==BASE+path and r.request.method=='POST')as response:
                await panel.get_by_role('button',name='Retry the same check request',exact=True).click()
            replay=await (await response.value).json();assert replay['check']['id']==original['check']['id']
            assert len(writes)==2 and writes[0]==writes[1]
            await page.unroute('**'+path,lost);report=await wait_report(page,media['id']);assert report['result']=='CALLABLE'
            history=await request(page,'/v1/operations/channels/'+media['id']+'/reports');assert len(history['items'])==1
            account=next(c for c in (await request(page,'/v1/operations/channels'))['items']if c['kind']=='ACCOUNT'and c['scope']['platform']=='douyin')
            account_card=panel.locator('[data-channel="'+account['id']+'"]')
            await account_card.get_by_label('Check this account identity online',exact=True).check()
            await account_card.get_by_label('I confirm a read-only identity check for this account. No login or publication will start.',exact=True).check()
            second=await context.new_page();await second.goto(BASE+'/settings')
            existing=next(a for a in (await request(second,'/v1/studio/accounts'))['items']if a['id']==account['scope']['account_id'])
            await request(second,'/v1/studio/accounts/'+existing['id'],{'label':existing['label']+' checked','tags':existing['tags'],'expected_version':existing['version']},'PUT')
            await expect(account_card.get_by_text('The channel changed while you reviewed it. Your online selection is retained; review the current version before checking.',exact=False)).to_be_visible(timeout=12000)
            await expect(account_card.get_by_label('Check this account identity online',exact=True)).to_be_checked()
            await account_card.get_by_role('button',name='Review current version',exact=True).click()
            await expect(account_card.get_by_role('button',name='Run read-only check',exact=True)).to_be_disabled()
            await account_card.get_by_label('I confirm a read-only identity check for this account. No login or publication will start.',exact=True).check()
            await account_card.get_by_role('button',name='Run read-only check',exact=True).click();report=await wait_report(page,account['id'])
            assert report['evidence_status']=='fixture_verified'and report['actual_operations']['check']==1
            assert all(report['actual_operations'][key]==0 for key in ('generate','paid_model','authorize','upload','query'))
            await account_card.get_by_role('button',name='Changes and recovery',exact=True).click()
            await expect(account_card.locator('.inspection-events')).to_be_visible()
            events=(await request(page,'/v1/operations/channel-events?channel_id='+account['id']))['items'];assert events and events[0]['kind']=='INITIAL_OBSERVATION'
            await account_card.get_by_role('button',name='View this report',exact=True).click();await expect(account_card.get_by_role('region',name='Selected historical report')).to_be_visible()
            await second.close()
        for locale in ('en','zh-Hans','ar','he'):
            await page.locator('#language-select').select_option(locale)
            for width in (390,1440):
                await page.set_viewport_size({'width':width,'height':1100})
                await expect(page.locator('#channel-inspections')).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(locale,width)
                await page.locator('#theme-select').select_option('dark');await page.locator('#theme-select').select_option('light')
        await page.locator('#language-select').select_option('en');await page.set_viewport_size({'width':390,'height':1100})
        await page.locator('#channel-inspections').get_by_role('button',name='Refresh observations',exact=True).focus();await page.keyboard.press('Tab')
        assert await page.evaluate('document.activeElement?.tagName')in ('BUTTON','INPUT','A')
        shots=os.environ.get('LOOPCAST_INSPECTION_SCREENSHOTS')
        if shots:
            root=Path(shots);root.mkdir(parents=True,exist_ok=True);mode='empty'if EMPTY else 'restored'if RESTORED else 'normal'
            await page.locator('#channel-inspections').screenshot(path=str(root/(mode+'-390.png')))
            await page.set_viewport_size({'width':1440,'height':1100});await page.locator('#channel-inspections').screenshot(path=str(root/(mode+'-1440.png')))
        after=await request(page,'/v1/operations/snapshot');assert before['database']['counts']==after['database']['counts']
        assert not forbidden,forbidden;assert not errors,errors;assert not external,external
        reads=[]
        page.on('request',lambda r:reads.append(r.url)if '/v1/operations/channels'in r.url else None)
        await page.goto(BASE+'/library');await page.wait_for_timeout(3500);count=len(reads);await page.wait_for_timeout(3500);assert len(reads)==count,'Inspection polling survived leaving the settings page'
        await browser.close()
    print('Installed inspections browser passed: '+('real empty configuration and no invented accounts'if EMPTY else 'old report scopes superseded and recovery barrier unchanged'if RESTORED else 'original request identity across reload, two-page version review, explicit account check, report/event links')+'; en/zh frozen RTL,390/1440,themes,keyboard,leave cleanup; no generation/login/upload')


asyncio.run(main())
