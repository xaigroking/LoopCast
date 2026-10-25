"""Installed frontend creation-to-publication flow with explicit isolated fixtures."""
import asyncio
import json
import os
from pathlib import Path
import time
from playwright.async_api import async_playwright, expect

BASE=os.environ['LOOPCAST_BASE_URL']
MEDIA=Path(os.environ['LOOPCAST_TEST_MEDIA'])


async def main():
    errors=[];external=[]
    async with async_playwright() as tool:
        browser=await tool.chromium.launch(channel='chromium')
        context=await browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
        page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:external.append(r.url) if not r.url.startswith(BASE) else None)
        await page.goto(BASE+'/library')
        await expect(page.get_by_role('heading',name='Media library',exact=True)).to_be_visible()
        await expect(page.get_by_text('No registered media yet.',exact=True)).to_be_visible()
        await page.locator('#media-file').set_input_files(MEDIA)
        await page.locator('#media-source').fill('Owned browser contract clip')
        await page.get_by_role('button',name='Import media',exact=True).click()
        await expect(page.locator('.asset-card')).to_have_count(1,timeout=15000)
        await page.locator('aside nav a[href="/create"]').click()
        await page.locator('#project-title').fill('Browser creation project')
        await page.get_by_role('button',name='New project',exact=True).click()
        await page.wait_for_url('**/create/*');project_url=page.url
        await page.locator('#creation-script').fill('First browser narration\n\nSecond browser narration')
        await page.get_by_role('button',name='Split into scenes',exact=True).click()
        await expect(page.locator('.scene')).to_have_count(2)
        await page.locator('.scene').nth(1).get_by_role('button',name='Move up',exact=True).click()
        await expect(page.locator('.scene').first.get_by_role('textbox',name='Narration',exact=True)).to_have_value('Second browser narration')
        await page.locator('#configure fieldset input[type=checkbox]').first.check()
        await page.locator('aside nav a[href="/settings"]').click()
        await page.locator('#theme-select').select_option('dark')
        await page.locator('#language-select').select_option('ar')
        await page.goto(project_url)
        await expect(page.locator('html')).to_have_attribute('dir','rtl')
        await expect(page.locator('#creation-script')).to_have_value('Second browser narration\n\nFirst browser narration')
        await page.reload()
        await expect(page.locator('#creation-script')).to_have_value('Second browser narration\n\nFirst browser narration')
        await page.locator('aside nav a[href="/settings"]').click()
        await page.locator('#language-select').select_option('en')
        await page.get_by_role('button',name='Run configuration diagnostics',exact=True).click()
        await expect(page.locator('#generator-diagnostics [role=status]')).to_contain_text('entry is callable')
        await page.goto(project_url)
        await page.get_by_role('button',name='Save draft',exact=True).click()
        await expect(page.locator('.heading p')).to_have_text('Draft · Saved')
        await page.get_by_role('button',name='Generate voice preview',exact=True).click()
        await expect(page.locator('.generation [data-state=SUCCEEDED]')).to_have_count(1,timeout=20000)
        await expect(page.locator('#preview audio')).to_have_count(1)
        await page.get_by_role('button',name='Generate video',exact=True).click()
        await expect(page.locator('.generation [data-state=SUCCEEDED]')).to_have_count(2,timeout=20000)
        await expect(page.locator('.real-video video')).to_be_visible()
        video=page.locator('.real-video video')
        await video.evaluate('v => v.play()')
        await page.wait_for_function("document.querySelector('.real-video video').readyState >= 2 && document.querySelector('.real-video video').videoWidth === 64")
        await video.evaluate('v => v.pause()')
        assert await video.evaluate('v => v.duration') > 0
        await page.get_by_role('link',name='Continue to publishing',exact=True).click()
        await page.wait_for_url('**/publish?asset=*')
        await expect(page.locator('#release-asset')).not_to_have_value('')
        await page.locator('#release-title').fill('Browser release')
        await page.locator('aside nav a[href="/accounts"]').click()
        for platform in ['douyin','xiaohongshu']:
            await page.get_by_role('button',name='Add account',exact=True).click()
            await page.get_by_role('dialog').get_by_role('combobox',name='Platform',exact=True).select_option(platform)
            await page.get_by_role('button',name='Start login',exact=True).click()
            await expect(page.locator('#contract-subject')).to_be_visible()
            await page.locator('#contract-subject').fill('test-'+platform)
            await page.get_by_role('button',name='Confirm test login',exact=True).click()
            await expect(page.get_by_role('dialog').locator('[data-state=SUCCEEDED]')).to_be_visible(timeout=15000)
            await page.get_by_role('dialog').get_by_role('button',name='Close',exact=True).click()
        await expect(page.locator('.account-card')).to_have_count(2)
        await page.locator('aside nav a[href="/publish"]').click()
        await expect(page.locator('#release-title')).to_have_value('Browser release')
        await page.locator('.target-choice input[type=checkbox]').nth(0).check()
        await page.locator('.target-choice input[type=checkbox]').nth(1).check()
        await page.locator('#release-preflight').click()
        await expect(page.get_by_role('heading',name='Preflight passed',exact=True)).to_be_visible()
        await page.locator('#release-confirm').check()
        await page.locator('#release-freeze').click()
        await expect(page.locator('.release-batch')).to_have_count(1)
        await page.locator('.release-target').filter(has_text='test-douyin').get_by_role('button',name='Send this target',exact=True).click()
        await expect(page.locator('.release-target').filter(has_text='test-douyin').locator('.section-head [data-state=PUBLISHED]')).to_be_visible(timeout=15000)
        await expect(page.locator('.release-batch > .section-head [data-state=PARTIAL]')).to_be_visible()
        await page.locator('.release-target').filter(has_text='test-xiaohongshu').get_by_role('button',name='Send this target',exact=True).click()
        target=page.locator('.release-target').filter(has_text='test-xiaohongshu')
        await expect(target.locator('.section-head [data-state=UNKNOWN]')).to_be_visible(timeout=15000)
        await expect(target.get_by_role('button',name='Send this target',exact=True)).to_have_count(0)
        await target.get_by_role('button',name='Verify result',exact=True).click()
        await expect(target.locator('.section-head [data-state=PUBLISHED]')).to_be_visible(timeout=15000)
        await expect(page.locator('.release-batch > .section-head [data-state=PUBLISHED]')).to_be_visible()
        await page.locator('#release-preflight').click()
        await expect(page.get_by_role('heading',name='Preflight blocked',exact=True)).to_be_visible()
        await expect(page.get_by_text('This account already published this media lineage. Permanently blocked.',exact=True)).to_have_count(2)
        # Primary languages and representative frozen RTL packs render the new screens.
        registry=await (await page.request.get(BASE+'/locales/registry.json')).json()
        for language in [item for item in registry if item['code'] in {'en','zh-Hans','ar','he'}]:
            await page.goto(BASE+'/settings')
            await page.locator('#language-select').select_option(language['code'])
            await expect(page.locator('html')).to_have_attribute('lang',language['code'])
            for path in ['/create','/library','/accounts','/publish',project_url.removeprefix(BASE)]:
                await page.goto(BASE+path)
                await expect(page.locator('main h1')).to_be_visible()
                assert not await page.locator('main').evaluate("n => /studio:|reason_[A-Z]|state_[A-Z]/.test(n.innerText)"),(language,path)
                await page.set_viewport_size({'width':390,'height':844})
                await page.wait_for_timeout(40)
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(language,path,'overflow')
                await page.set_viewport_size({'width':1440,'height':1000})
        assert not errors,errors
        assert not external,external
        await browser.close()
    print('Installed studio browser passed: creation/revisions/audio/video/playback/handoff, account authorization, per-target partial/unknown/query/permanent guard, English/Simplified Chinese and representative frozen RTL packs at 390/1440; no external requests')

if __name__=='__main__':asyncio.run(main())
