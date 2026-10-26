"""Installed normal/recovered workspaces: read-only operations and explicit barrier."""
import asyncio
import json
import os
import re
from playwright.async_api import async_playwright, expect

BASE = os.environ['LOOPCAST_BASE_URL']
GUARDED = os.environ.get('LOOPCAST_EXPECT_RESTORE_GUARD') == '1'


async def main():
    errors=[];external=[]
    async with async_playwright() as tool:
        browser=await tool.chromium.launch(channel='chromium');context=await browser.new_context(locale='en-US',viewport={'width':1440,'height':1000});page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:external.append(r.url) if not r.url.startswith(BASE) else None)
        await page.goto(BASE+'/settings');panel=page.locator('#operations')
        await expect(panel.get_by_role('heading',name='Operations and recovery',exact=True)).to_be_visible()
        await expect(panel.get_by_text('Ready',exact=True)).to_be_visible()
        status=await (await page.request.get(BASE+'/v1/operations/status')).json()
        assert status['service_ready'] is True and bool(status['restore_guard']) is GUARDED
        if GUARDED:
            assert status['configured_sending_enabled'] is True and status['effective_sending_enabled'] is False
            await expect(panel.locator('.restore-guard')).to_be_visible();await expect(panel.get_by_text('Blocked',exact=True)).to_be_visible()
        else:await expect(panel.locator('.restore-clear')).to_be_visible()
        snapshot=panel.get_by_role('button',name='Read runtime snapshot',exact=True)
        await snapshot.focus();await page.keyboard.press('Enter')
        await expect(panel.locator('.operations-snapshot')).to_be_visible()
        await panel.get_by_role('button',name='Read reconciliation report',exact=True).press('Enter')
        await expect(panel.locator('.operations-reconciliation')).to_be_visible()
        report=await (await page.request.get(BASE+'/v1/operations/snapshot')).json()
        assert report['database']['schema']['value']==8 and report['application']['build_source']['status']=='unavailable'
        assert report['recovery']['guard']['value']==('ACTIVE' if GUARDED else 'CLEAR')
        assert report['runtime']['service_ready']['value'] is True
        encoded=json.dumps(report)
        for private in ('contract-provider-canary','private-credential-canary','config_ref','credentials.json'):assert private not in encoded
        assert await panel.locator('input[type=file]').count()==0
        assert await panel.get_by_role('button',name=re.compile('unlock|force|解除|强制',re.I)).count()==0
        await page.goto(BASE+'/publish');await expect(page.locator('#operations')).to_be_visible()
        if GUARDED:
            await expect(page.locator('#operations .restore-guard')).to_be_visible()
            assert await page.get_by_role('button',name='Send this target',exact=True).count()==0
        await page.get_by_role('link',name='Open recovery status and reports',exact=True).click()
        await expect(page.locator('#operations')).to_be_visible()
        for locale in ('en','zh-Hans','ar','he'):
            await page.goto(BASE+'/settings');await page.locator('#language-select').select_option(locale)
            await expect(page.locator('html')).to_have_attribute('lang',locale)
            title='运维与恢复' if locale=='zh-Hans' else 'Operations and recovery'
            await expect(panel.get_by_role('heading',name=title,exact=True)).to_be_visible()
            await panel.get_by_role('button',name='读取运行快照' if locale=='zh-Hans' else 'Read runtime snapshot',exact=True).click()
            await expect(panel.locator('.operations-snapshot')).to_be_visible()
            await panel.get_by_role('button',name='读取对账报告' if locale=='zh-Hans' else 'Read reconciliation report',exact=True).click()
            await expect(panel.locator('.operations-reconciliation')).to_be_visible()
            for width in (390,1440):
                await page.set_viewport_size({'width':width,'height':1000})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(locale,width)
            assert not await panel.evaluate("n=>/studio:|observationState_|count_feedback/.test(n.innerText)")
        assert not errors,errors;assert not external,external
        await browser.close()
    print('Installed operations browser passed: '+('recovered barrier with ready service and blocked uploads' if GUARDED else 'normal workspace')+'; read-only snapshot/reconciliation, keyboard actions, primary languages and frozen RTL at 390/1440; no platform calls')


if __name__=='__main__':asyncio.run(main())
