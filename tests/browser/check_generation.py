"""Installed media generation and ambiguous publication evidence in Chromium."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from playwright.async_api import async_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from generation_support import settings
from support import ServerProcess


async def main():
    with tempfile.TemporaryDirectory() as directory:
        config=settings(directory);path=Path(directory)/'generator.json';path.write_text(json.dumps(config))
        server=ServerProcess(Path(directory)/'data',path).start()
        try:
            async with async_playwright() as tool:
                browser=await tool.chromium.launch(channel='chromium')
                for width in [1440,390]:
                    context=await browser.new_context(viewport={'width':width,'height':1000})
                    page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    await page.goto(server.url)
                    await page.get_by_role('button',name='+ New fixture',exact=True).click()
                    await page.locator('select[name=mode]').select_option('mpt')
                    await page.get_by_label('Task name').fill('Browser video '+str(width))
                    await page.get_by_role('button',name='Generate video →',exact=True).click()
                    await page.wait_for_url('**/runs/*')
                    await expect(page.locator('video')).to_be_visible(timeout=15000)
                    await page.locator('video').evaluate('(video) => video.play()')
                    await expect(page.locator('video')).to_have_js_property('videoWidth',64,timeout=10000)
                    await page.wait_for_function('document.querySelector("video").currentTime > 0')
                    await expect(page.get_by_text('MPT · contract tested, live unverified',exact=True)).to_be_visible()
                    await page.get_by_label('Simulator account',exact=True).fill('account-'+str(width))
                    await page.get_by_role('button',name='Add simulated target',exact=True).click()
                    target=page.locator('[data-intent-id]').first
                    await expect(target.get_by_role('button',name='Dispatch simulation',exact=True)).to_be_enabled()
                    await target.locator('select').select_option('lost_response')
                    await target.get_by_role('button',name='Dispatch simulation',exact=True).click()
                    await expect(target.locator('.state')).to_have_text('UNKNOWN',timeout=10000)
                    await expect(target.get_by_role('button',name='Dispatch simulation',exact=True)).to_be_disabled()
                    await target.get_by_role('button',name='Check simulator receipt',exact=True).click()
                    await expect(target.locator('.state')).to_have_text('PUBLISHED')
                    await expect(target.get_by_role('button',name='Dispatch simulation',exact=True)).to_be_disabled()
                    await expect(target).to_contain_text('simulator-query')
                    identity=page.url
                    await page.reload();await expect(page.locator('[data-intent-id] .state')).to_have_text('PUBLISHED')
                    assert page.url==identity
                    assert not await page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    assert not errors,errors
                    await context.close()
                await browser.close()
        finally:server.stop()
    print('Generation browser acceptance passed: installed MP4, 1440/390px, simulated UNKNOWN, receipt query, blocked resend, reload.')

asyncio.run(main())
