"""Installed manual-feedback UI using only the explicit contract publication fixture."""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from playwright.async_api import async_playwright, expect

BASE=os.environ['LOOPCAST_BASE_URL']


async def main():
    errors=[];external=[]
    async with async_playwright() as tool:
        browser=await tool.chromium.launch(channel='chromium')
        context=await browser.new_context(viewport={'width':1440,'height':1000},locale='en-US');page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:external.append(r.url) if not r.url.startswith(BASE) else None)
        batches=await (await page.request.get(BASE+'/v1/studio/releases')).json()
        batch=next(b for b in batches['items'] if b['title']=='Browser release');target=next(t for t in batch['targets'] if t['platform']=='douyin')
        assert target['state']=='PUBLISHED' and target['evidence_mode']=='contract'
        work=next(r['work_id'] for r in target['receipts'] if r['work_id'])
        document={'format':'loopcast.metrics.v1','source':'Browser manual fixture, platform counters','evidence_mode':'contract','rows':[{'platform':target['platform'],'stable_subject':target['stable_subject'],'work_id':work,'asset_id':batch['asset_id'],'observed_at':datetime.now(timezone.utc).isoformat(),'aggregation':'cumulative','definition':'platform_reported_v1','metrics':{'views':1200,'likes':None,'watch_seconds':12.5}}]}
        raw=json.dumps(document,indent=2).encode()
        await page.goto(BASE+'/publish');await page.get_by_role('link',name='Feedback and decisions',exact=True).click()
        await expect(page.get_by_role('heading',name='Feedback and decisions',exact=True)).to_be_visible()
        await page.locator('#observations-file').set_input_files({'name':'browser-observations.json','mimeType':'application/json','buffer':raw})
        await page.get_by_role('button',name='Import observations',exact=True).click()
        await expect(page.locator('.import-report')).to_have_count(1)
        await page.locator('.import-report summary').click()
        await expect(page.get_by_text('Create an explicit work binding, then reprocess',exact=True)).to_be_visible()
        await page.get_by_role('combobox',name='Existing publication target',exact=True).select_option(target['id'])
        await page.locator('#binding-published').fill((datetime.now(timezone.utc)-timedelta(hours=48)).isoformat())
        await page.locator('#binding-source').fill('Synthetic browser contract publication age')
        await page.locator('#binding-confirm').check()
        await page.get_by_role('button',name='Bind a platform work',exact=True).click()
        await expect(page.get_by_text('Binding backed by a platform receipt',exact=False)).to_be_visible()
        await page.get_by_role('button',name='Reprocess after binding',exact=True).click()
        await expect(page.get_by_text('Accepted',exact=True)).to_be_visible()
        await expect(page.locator('.snapshot')).to_have_count(1)
        state=await (await page.request.get(BASE+'/v1/feedback')).json();report=state['imports'][0]
        assert report['sha256']==hashlib.sha256(raw).hexdigest()
        original=await page.request.get(BASE+'/v1/feedback/imports/'+report['id']+'/original');assert await original.body()==raw
        # A second exact upload remains one import and one observation.
        await page.get_by_role('button',name='Import observations',exact=True).click()
        await expect(page.locator('.import-report')).to_have_count(1);await expect(page.locator('.snapshot')).to_have_count(1)
        await page.locator('.snapshot input[type=checkbox]').check()
        await page.locator('#decision-topic').fill('Evidence-linked second round')
        await page.get_by_role('button',name='Propose decision',exact=True).click()
        card=page.locator('.decision-card').first
        await expect(card.get_by_text('Reference the eligible work with the highest observed views',exact=True)).to_be_visible()
        await card.get_by_role('textbox',name='Next topic',exact=True).fill('Reviewed second round')
        await card.get_by_role('checkbox').check()
        await card.get_by_role('button',name='Confirm as a new revision',exact=True).click()
        await card.get_by_role('link',name='Open next-round revision',exact=False).click()
        await expect(page.get_by_role('heading',name='Reviewed second round',exact=True)).to_be_visible()
        await expect(page.get_by_role('link',name='Source decision and snapshots',exact=True)).to_be_visible()
        await page.locator('#creation-script').fill('Explicit second-round narration from reviewed feedback.')
        await page.get_by_role('button',name='Save draft',exact=True).click();await expect(page.locator('.heading p')).to_have_text('Draft · Saved')
        await page.get_by_role('button',name='Save revision',exact=True).click()
        await page.reload();await expect(page.locator('#creation-script')).to_have_value('Explicit second-round narration from reviewed feedback.')
        await expect(page.get_by_role('link',name='Source decision and snapshots',exact=True)).to_be_visible()
        decisions=await (await page.request.get(BASE+'/v1/feedback/decisions')).json();decision=decisions[0]
        assert decision['input']['topic']=='Evidence-linked second round' and len(decision['revisions'])==2
        assert decision['selected_snapshot_id']==state['snapshots'][0]['id']
        for language in ['zh-Hans','ar','he','en']:
            await page.goto(BASE+'/settings');await page.locator('#language-select').select_option(language)
            await expect(page.locator('html')).to_have_attribute('lang',language)
            await page.goto(BASE+'/feedback');await expect(page.locator('main h1')).to_be_visible()
            assert not await page.locator('main').evaluate("n=>/studio:|feedback_code_/.test(n.innerText)")
            await page.set_viewport_size({'width':390,'height':844});assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),language
            await page.set_viewport_size({'width':1440,'height':1000})
        assert not errors,errors;assert not external,external
        await browser.close()
    print('Installed feedback browser passed: explicit work binding, original bytes/digest, quarantine/reprocess, duplicate import, snapshot selection, edited decision/frozen revision/provenance and primary/RTL layouts; contract evidence only')

if __name__=='__main__':asyncio.run(main())
