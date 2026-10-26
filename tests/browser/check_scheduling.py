"""Installed finite-plan UI: explicit consent, lost responses, conflicts and restore."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from playwright.async_api import async_playwright, expect

BASE=os.environ['LOOPCAST_BASE_URL']
GUARDED=os.environ.get('LOOPCAST_EXPECT_RESTORE_GUARD')=='1'


async def request(page,path,body=None):
    result=await page.evaluate('''async ({path,body,key})=>{const response=await fetch(path,{method:body===null?'GET':'POST',headers:body===null?{}:{'Content-Type':'application/json','Idempotency-Key':key},body:body===null?undefined:JSON.stringify(body)});return {status:response.status,value:await response.json()}}''',{'path':path,'body':body,'key':uuid.uuid4().hex})
    assert result['status']==200,(path,result)
    return result['value']


async def authorize(page,subject):
    await page.goto(BASE+'/accounts')
    await page.get_by_role('button',name='Add account',exact=True).click()
    dialog=page.get_by_role('dialog')
    await dialog.get_by_role('combobox',name='Platform',exact=True).select_option('douyin')
    await dialog.get_by_role('button',name='Start login',exact=True).click()
    await expect(page.locator('#contract-subject')).to_be_visible()
    await page.locator('#contract-subject').fill(subject)
    await dialog.get_by_role('button',name='Confirm test login',exact=True).click()
    await expect(dialog.locator('[data-state=SUCCEEDED]')).to_be_visible(timeout=15000)
    await dialog.get_by_role('button',name='Close',exact=True).click()
    return next(a for a in (await request(page,'/v1/studio/accounts'))['items'] if a['stable_subject']==subject)


async def frozen_release(page,account,title):
    asset=next(a for a in (await request(page,'/v1/studio/assets'))['items'] if a['kind']=='video')
    value={'asset_id':asset['id'],'title':title,'description':'','cover_id':None,
           'targets':[{'account_id':account['id'],'fields':{'category':None,'visibility':'public','declaration':None}}]}
    check=await request(page,'/v1/studio/releases/preflight',value);assert check['allowed'],check
    return await request(page,'/v1/studio/releases',value|{'preflight_digest':check['digest'],'confirmed':True})


async def main():
    errors=[];external=[]
    async with async_playwright() as tool:
        browser=await tool.chromium.launch(channel='chromium')
        context=await browser.new_context(locale='en-US',viewport={'width':1440,'height':1000});page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:external.append(r.url) if not r.url.startswith(BASE) else None)
        await page.goto(BASE+'/settings');await page.locator('#language-select').select_option('en')
        if GUARDED:
            await page.goto(BASE+'/publish#schedules');panel=page.locator('#schedules')
            await expect(panel.get_by_role('heading',name='Publication plans',exact=True)).to_be_visible()
            cap=await request(page,'/v1/studio/schedules/capabilities');assert cap['enabled'] and not cap['effective_sending_enabled']
            plans=(await request(page,'/v1/studio/schedules'))['items']
            restored=next(p for p in plans if p['pause_reason']=='RESTORE_GUARD')
            card=page.locator('[data-schedule="'+restored['id']+'"]')
            await expect(card.get_by_text('Recovery requires publication reconciliation; plans stay paused.',exact=True)).to_be_visible()
            await card.get_by_role('button',name='Preview times and checks',exact=True).click()
            await expect(card.locator('.schedule-preview')).to_be_visible()
            await expect(card.locator('.schedule-enable')).to_be_disabled()
            assert not any(p['control_state']=='ACTIVE' for p in plans)
            assert not errors,errors
            await browser.close()
            print('Installed scheduling browser passed: restored plans paused, recovery reasons visible, enable blocked despite ready service and configured policy')
            return
        first_account=await authorize(page,'plan-douyin')
        first_release=await frozen_release(page,first_account,'Scheduled browser video 1')
        await page.goto(BASE+'/publish#schedules');panel=page.locator('#schedules')
        await expect(panel.get_by_role('heading',name='Publication plans',exact=True)).to_be_visible()
        await panel.locator('.schedule-choices').get_by_label(re.compile('plan-douyin')).check()
        due=datetime.fromtimestamp(math.ceil((time.time()+120)/60)*60,timezone.utc)
        await page.locator('#schedule-date').fill(due.date().isoformat())
        await page.locator('#schedule-time').fill(due.strftime('%H:%M'))
        await page.locator('#schedule-zone').fill('UTC')
        await page.locator('#schedule-preview').focus();await page.keyboard.press('Enter')
        await expect(panel.locator('.schedule-composer .schedule-preview')).to_be_visible()
        await expect(page.locator('#schedule-create')).to_be_disabled()
        await page.locator('#schedule-confirm').check()
        calls=[];aborted=False
        async def lose_response(route):
            nonlocal aborted
            if route.request.method!='POST':await route.continue_();return
            calls.append(route.request.headers.get('idempotency-key'))
            if not aborted:
                aborted=True
                response=await route.fetch();assert response.status==200
                await route.abort('failed')
            else:await route.continue_()
        await page.route('**/v1/studio/schedules',lose_response)
        await page.locator('#schedule-create').click()
        await expect(panel.get_by_role('button',name='Retry pending action',exact=True)).to_be_visible()
        plans=(await request(page,'/v1/studio/schedules'))['items'];assert len(plans)==1 and plans[0]['control_state']=='PAUSED'
        identity=plans[0]['id']
        await page.reload();panel=page.locator('#schedules')
        await expect(panel.get_by_role('button',name='Retry pending action',exact=True)).to_be_visible()
        assert len(calls)==1
        await panel.get_by_role('button',name='Retry pending action',exact=True).click()
        await expect(panel.get_by_role('button',name='Retry pending action',exact=True)).to_have_count(0)
        assert len(calls)==2 and calls[0]==calls[1]
        await page.unroute('**/v1/studio/schedules',lose_response)
        assert len((await request(page,'/v1/studio/schedules'))['items'])==1
        card=page.locator('[data-schedule="'+identity+'"]')
        await expect(card.locator('[data-state=PAUSED]').first).to_be_visible()
        # A second page retains its own draft when the first changes the revision.
        second=await context.new_page();second.on('pageerror',lambda e:errors.append(str(e)))
        await second.goto(BASE+'/publish#schedules');other=second.locator('[data-schedule="'+identity+'"]')
        await other.get_by_role('button',name='Edit future time and limits',exact=True).click()
        old_time=await other.locator('input[type=time]').input_value()
        await card.get_by_role('button',name='Edit future time and limits',exact=True).click()
        final_due=datetime.fromtimestamp(math.ceil((time.time()+30)/60)*60,timezone.utc)
        if final_due.date()<due.date():final_due=datetime.combine(due.date(),datetime.min.time(),timezone.utc)
        assert final_due.date()==due.date()
        await card.locator('input[type=time]').fill(final_due.strftime('%H:%M'))
        await card.get_by_role('button',name='Preview times and checks',exact=True).click()
        await expect(card.locator('.schedule-preview')).to_be_visible()
        await card.get_by_role('button',name='Save future revision, keep paused',exact=True).click()
        await expect(card.get_by_text('Rule revision 2',exact=False)).to_be_visible()
        await other.get_by_role('button',name='Preview times and checks',exact=True).click()
        await expect(other.locator('[role=alert]')).to_be_visible()
        await expect(other.locator('input[type=time]')).to_have_value(old_time)
        await second.close()
        await card.get_by_role('button',name='Preview times and checks',exact=True).click()
        await expect(card.locator('.schedule-preview')).to_be_visible()
        await expect(card.locator('.schedule-enable')).to_be_disabled()
        await card.locator('.schedule-enable-confirm').check()
        await card.locator('.schedule-enable').click()
        await expect(card.locator('[data-state=PUBLISHED]').first).to_be_visible(timeout=130000)
        await card.get_by_role('button',name='Occurrence history',exact=True).click()
        await expect(card.locator('[data-occurrence]')).to_have_count(1)
        await card.get_by_role('link',name='View original target, attempts and receipts',exact=True).click()
        await expect(page.locator('#target-'+first_release['targets'][0]['id']).locator('.section-head [data-state=PUBLISHED]')).to_be_visible()
        original=await request(page,'/v1/studio/releases/'+first_release['id'])
        assert len(original['targets'][0]['attempts'])==1 and len(original['targets'][0]['receipts'])==1
        # Leave a second explicitly enabled, future plan for actual backup/restore.
        account=await authorize(page,'plan-second')
        await frozen_release(page,account,'Scheduled browser video 2')
        await page.goto(BASE+'/publish#schedules');panel=page.locator('#schedules')
        remove=panel.locator('.schedule-queue').get_by_role('button',name='Remove from this draft',exact=True)
        if await remove.count():await remove.click()
        await panel.locator('.schedule-choices').get_by_label(re.compile('plan-second')).check()
        day=(datetime.now(timezone.utc).date()+timedelta(days=1)).isoformat()
        await page.locator('#schedule-date').fill(day);await page.locator('#schedule-time').fill('09:00')
        await page.locator('#schedule-preview').click();await expect(panel.locator('.schedule-composer .schedule-preview')).to_be_visible()
        await page.locator('#schedule-confirm').check();await page.locator('#schedule-create').click()
        await expect(panel.locator('.schedule-card')).to_have_count(2)
        second_plan=next(p for p in (await request(page,'/v1/studio/schedules'))['items'] if p['id']!=identity)
        card=page.locator('[data-schedule="'+second_plan['id']+'"]')
        await card.get_by_role('button',name='Preview times and checks',exact=True).click();await expect(card.locator('.schedule-preview')).to_be_visible()
        await card.locator('.schedule-enable-confirm').check();await card.locator('.schedule-enable').click()
        await expect(card.locator('[data-state=ACTIVE]').first).to_be_visible()
        for locale in ('en','zh-Hans','ar','he'):
            await page.goto(BASE+'/settings');await page.locator('#language-select').select_option(locale)
            await page.locator('#theme-select').select_option('dark' if locale in ('ar','he') else 'light')
            await page.goto(BASE+'/publish#schedules');panel=page.locator('#schedules')
            await expect(panel.get_by_role('heading',name='发布计划' if locale=='zh-Hans' else 'Publication plans',exact=True)).to_be_visible()
            for width in (390,1440):
                await page.set_viewport_size({'width':width,'height':1000})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(locale,width)
            if locale in ('ar','he'):await expect(page.locator('html')).to_have_attribute('dir','rtl')
            assert not await panel.evaluate("n=>/studio:|schedReason_|state_WAITING/.test(n.innerText)")
        screenshots=os.environ.get('LOOPCAST_SCHEDULE_SCREENSHOTS')
        if screenshots:
            root=Path(screenshots);root.mkdir(parents=True,exist_ok=True)
            await page.goto(BASE+'/settings');await page.locator('#language-select').select_option('en');await page.locator('#theme-select').select_option('light')
            for width in (390,1440):
                await page.set_viewport_size({'width':width,'height':1000});await page.goto(BASE+'/publish#schedules')
                await page.locator('#schedules').scroll_into_view_if_needed()
                await page.screenshot(path=str(root/f'scheduling-{width}.png'))
        assert not errors,errors;assert not external,external
        await browser.close()
    print('Installed scheduling browser passed: frozen targets, paused consent, lost-response identity across reload, two-page conflict/draft, version edit, one-time publication/history, future plan, en/zh frozen RTL, 390/1440 and themes; no real platform calls')


if __name__=='__main__':asyncio.run(main())
