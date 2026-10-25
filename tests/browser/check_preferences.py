"""Language resources, browser preferences and draft-preserving transitions."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from playwright.async_api import async_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from support import ServerProcess


async def main():
    with tempfile.TemporaryDirectory() as directory:
        server=ServerProcess(directory).start()
        try:
            async with async_playwright() as tool:
                browser=await tool.chromium.launch(channel='chromium')
                rows=server.request('/locales/registry.json')[1]
                assert len(rows)==20
                for row in rows:
                    context=await browser.new_context(locale=row['code'],viewport={'width':390,'height':950})
                    page=await context.new_page();await page.goto(server.url+'/settings')
                    await expect(page.locator('html')).to_have_attribute('lang',row['code'])
                    await expect(page.locator('html')).to_have_attribute('dir',row['dir'])
                    data=server.request('/locales/'+row['code']+'.json')[1]
                    await expect(page.locator('h1')).to_have_text(data['ui']['settings'])
                    cats=await page.evaluate('new Intl.PluralRules(document.documentElement.lang).resolvedOptions().pluralCategories')
                    assert all('taskCount_'+c in data['ui'] for c in cats),(row,cats)
                    for count in [0,1,2,3,5,11,21,1.5,1000000]:
                        value=await page.evaluate('(count)=>i18next.t("ui:taskCount",{count})',count)
                        assert '{{' not in value and 'taskCount' not in value,(row,value)
                    if row['code']!='en':assert data['ui']['console']!='Your task console'
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),row
                    await context.close()
                # Ordered browser matching, script/region mapping and English fallback.
                for languages,expected in [(['xx','en-US','de'],'en'),(['zh-TW','en'],'zh-Hant'),(['zh-SG'],'zh-Hans'),(['xx'],'en'),(['de-DE','en-US'],'de')]:
                    context=await browser.new_context()
                    await context.add_init_script('Object.defineProperty(navigator,"languages",{get:()=>'+json.dumps(languages)+'});')
                    page=await context.new_page();await page.goto(server.url+'/settings')
                    await expect(page.locator('html')).to_have_attribute('lang',expected)
                    await context.close()
                context=await browser.new_context(locale='en-US');page=await context.new_page()
                await page.goto(server.url)
                await page.get_by_role('button',name='+ New fixture',exact=True).click()
                await page.get_by_label('Task name').fill('Keep 私有 title')
                await page.get_by_label('Scenario').select_option('fail_once')
                await page.locator('#dialog-settings').click()
                await page.locator('#language-select').select_option('zh-Hant')
                await expect(page.locator('html')).to_have_attribute('lang','zh-Hant')
                await page.go_back()
                await page.locator('.heading .primary').click()
                await expect(page.locator('input[name=title]')).to_have_value('Keep 私有 title')
                await expect(page.locator('select[name=scenario]')).to_have_value('fail_once')
                await page.locator('#dismiss').click()
                other=await context.new_page();await other.goto(server.url+'/settings')
                await expect(other.locator('html')).to_have_attribute('lang','zh-Hant')
                await other.locator('#language-select').select_option('ar')
                await expect(page.locator('html')).to_have_attribute('lang','ar')
                before=server.request('/v1/runs?limit=100')[1]
                await other.reload();await expect(other.locator('html')).to_have_attribute('lang','ar')
                assert server.request('/v1/runs?limit=100')[1]==before
                await context.close()
                for script in ["Storage.prototype.getItem=()=>{throw Error('blocked')};Storage.prototype.setItem=()=>{throw Error('blocked')}","localStorage.setItem('loopcast.preferences','broken')"]:
                    context=await browser.new_context(locale='ja');await context.add_init_script(script)
                    page=await context.new_page();await page.goto(server.url+'/settings')
                    await expect(page.locator('html')).to_have_attribute('lang','ja')
                    await page.locator('#language-select').select_option('de')
                    await expect(page.locator('html')).to_have_attribute('lang','de')
                    await context.close()
                await browser.close()
        finally:server.stop()
    print('Preferences browser passed: 20 locales, CLDR/Intl, ordered detection, CJK/RTL, manual/storage synchronization and preserved form drafts.')

asyncio.run(main())
