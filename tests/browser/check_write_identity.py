"""Real writes under double clicks, lost responses, locale changes and two tabs."""
import asyncio
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
                context=await browser.new_context(locale='en-US');page=await context.new_page();page.on('dialog',lambda d:d.accept())
                captured=[]
                async def delayed(route):
                    if route.request.method!='POST':return await route.continue_()
                    captured.append((route.request.headers['idempotency-key'],route.request.post_data))
                    response=await route.fetch();await asyncio.sleep(.2);await route.fulfill(response=response)
                await page.route('**/v1/runs',delayed)
                await page.goto(server.url);await page.locator('.heading .primary').click()
                await page.locator('input[name=title]').fill('Double click')
                await page.locator('#create-form button[type=submit]').dblclick(delay=20)
                await page.wait_for_url('**/runs/*');assert len(captured)==1
                assert len([r for r in server.request('/v1/runs?limit=100')[1]['items'] if r['input']['title']=='Double click'])==1
                await page.unroute('**/v1/runs',delayed)
                for change in [False,True]:
                    captured=[]
                    async def lost(route):
                        if route.request.method!='POST':return await route.continue_()
                        captured.append((route.request.headers['idempotency-key'],route.request.post_data))
                        response=await route.fetch()
                        if len(captured)==1:return await route.abort()
                        await route.fulfill(response=response)
                    await page.route('**/v1/runs',lost)
                    await page.goto(server.url);await page.locator('.heading .primary').click()
                    await page.locator('input[name=title]').fill('Lost '+str(change))
                    await page.locator('#create-form button[type=submit]').click()
                    await expect(page.locator('#form-error')).not_to_be_empty()
                    await page.evaluate("workspacePreferences.set({language:'zh-Hans',theme:'dark'})")
                    await expect(page.locator('html')).to_have_attribute('lang','zh-Hans')
                    await expect(page.locator('input[name=title]')).to_have_value('Lost '+str(change))
                    if change:await page.locator('input[name=title]').fill('Changed explicit input')
                    await page.locator('#create-form button[type=submit]').click();await page.wait_for_url('**/runs/*')
                    assert len(captured)==2
                    assert (captured[0]==captured[1]) is (not change)
                    assert (captured[0][0]==captured[1][0]) is (not change)
                    await page.unroute('**/v1/runs',lost)
                await page.evaluate("workspacePreferences.set({language:'en'})")
                _,run=server.request('/v1/runs',{'title':'Two windows','scenario':'fail_once','delay_ms':200},'windows')
                failed=server.wait(run['run_id'],{'FAILED'});identity=run['run_id'];url=server.url+'/runs/'+identity
                first=await context.new_page();second=await context.new_page();await first.goto(url);await second.goto(url)
                async def stale(route):await route.fulfill(json=failed)
                await second.route('**/v1/runs/'+identity,stale)
                async with first.expect_response(lambda r:r.url.endswith('/retry')) as accepted:
                    await first.get_by_role('button',name='Retry',exact=True).click()
                assert (await accepted.value).status==202
                async with second.expect_response(lambda r:r.url.endswith('/retry')) as conflicted:
                    await second.get_by_role('button',name='Retry',exact=True).click()
                assert (await conflicted.value).status==409
                await second.unroute('**/v1/runs/'+identity,stale)
                final=server.wait(identity,{'SUCCEEDED'});assert len(final['attempts'])==2
                await second.reload();await expect(second.locator('.heading > .row-actions > .state')).to_have_text('SUCCEEDED')
                # Non-business account/scenario drafts survive locale, theme and server events.
                await second.locator('input[data-draft-key]').fill('retained_account')
                await second.evaluate("workspacePreferences.set({language:'ar',theme:'light'})")
                await expect(second.locator('html')).to_have_attribute('dir','rtl')
                await expect(second.locator('input[data-draft-key]')).to_have_value('retained_account')
                assert len(server.request('/v1/runs/'+identity)[1]['attempts'])==2
                await browser.close()
        finally:server.stop()
    print('Write identity browser passed: double click, response loss across language/theme changes, changed payload keys, two-window optimistic conflict, preserved account draft.')

asyncio.run(main())
