"""One brand geometry, installed resource variants and actual application views."""
import asyncio
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET
from playwright.async_api import async_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from support import ServerProcess


async def main():
    with tempfile.TemporaryDirectory() as directory:
        server=ServerProcess(directory).start()
        try:
            geometry=None
            for variant in ['light','dark','mono']:
                for size in [16,24,32,128]:
                    status,content=server.request(f'/brand.svg?variant={variant}&size={size}')
                    assert status==200
                    svg=ET.fromstring(content);assert svg.attrib['width']==str(size)
                    paths=[p.attrib['d'] for p in svg.iter('{http://www.w3.org/2000/svg}path')]
                    assert len(paths)==6
                    if geometry is None:geometry=paths
                    assert paths==geometry
            assert server.request('/brand.svg?size=0')[0]==400
            assert server.request('/v1/unknown')[0]==404
            for name,scenario in [('Report preview','success'),('Retry example','fail'),('Control-plane check','success')]:
                _,run=server.request('/v1/runs',{'title':name,'scenario':scenario},name)
                server.wait(run['run_id'],{'SUCCEEDED','FAILED'})
            async with async_playwright() as tool:
                browser=await tool.chromium.launch(channel='chromium')
                context=await browser.new_context(locale='en-US',viewport={'width':1440,'height':1050})
                page=await context.new_page();requested=[];page.on('request',lambda r:requested.append(r.url))
                await page.goto(server.url)
                await expect(page.locator('.run-card')).to_have_count(3)
                for theme in ['light','dark']:
                    await page.evaluate('(theme)=>workspacePreferences.set({theme})',theme)
                    await expect(page.locator('html')).to_have_attribute('data-theme',theme)
                    await page.screenshot(path='/tmp/loopcast-workspace-'+theme+'.png',full_page=True)
                for width,lang in [(1440,'zh-Hans'),(390,'ar')]:
                    await page.set_viewport_size({'width':width,'height':1050})
                    await page.goto(server.url+'/settings')
                    await page.locator('#language-select').select_option(lang)
                    await expect(page.locator('html')).to_have_attribute('lang',lang)
                    sources=await page.locator('img.brand-image').evaluate_all('(images)=>images.map(i=>i.getAttribute("src"))')
                    assert len(sources)==3 and set(sources)=={'/brand.svg'}
                    assert await page.locator('img.brand-image').first.evaluate('i=>getComputedStyle(i).transform')=='none'
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    assert '◈' not in await page.locator('body').inner_text()
                    await page.screenshot(path=f'/tmp/loopcast-settings-{lang}.png',full_page=True)
                assert any('/brand.svg' in url for url in requested)
                assert await page.locator('link[rel=icon]').count()==3
                await browser.close()
        finally:server.stop()
    print('Brand browser passed: single six-ribbon vector, identical light/dark/mono 16/24/32/128 geometry, sidebar/header/settings/favicon, offline installed resources and RTL screenshots.')

asyncio.run(main())
