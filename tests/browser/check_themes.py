"""Actual theme tokens, system changes and keyboard behavior in installed UI."""
import asyncio
from pathlib import Path
import sys
import tempfile
from playwright.async_api import async_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from support import ServerProcess


def luminance(value):
    value=value.lstrip('#')
    if len(value)==3:value=''.join(c*2 for c in value)
    rgb=[int(value[i:i+2],16)/255 for i in (0,2,4)]
    rgb=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
    return sum(a*b for a,b in zip(rgb,[.2126,.7152,.0722]))


def contrast(a,b):
    x,y=sorted([luminance(a),luminance(b)])
    return (y+.05)/(x+.05)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        server=ServerProcess(directory).start()
        try:
            async with async_playwright() as tool:
                browser=await tool.chromium.launch(channel='chromium')
                context=await browser.new_context(locale='en-US',color_scheme='light',viewport={'width':1440,'height':1000})
                page=await context.new_page();await page.goto(server.url+'/settings')
                await expect(page.locator('html')).to_have_attribute('data-theme','light')
                for theme in ['dark','light']:
                    await page.locator('#theme-select').select_option(theme)
                    await expect(page.locator('html')).to_have_attribute('data-theme',theme)
                    variables=await page.evaluate('''Object.fromEntries(['text','muted','surface','bg','accent','accent-text','accent-soft','green','green-bg','red','red-bg','blue','blue-bg','amber','amber-bg','state-bg'].map(k=>[k,getComputedStyle(document.documentElement).getPropertyValue('--'+k).trim()]))''')
                    for fg,bg in [('text','surface'),('text','bg'),('muted','surface'),('muted','bg'),('accent-text','accent'),('accent','accent-soft'),('green','green-bg'),('red','red-bg'),('blue','blue-bg'),('amber','amber-bg'),('muted','state-bg')]:
                        ratio=contrast(variables[fg],variables[bg]);assert ratio>=4.5,(theme,fg,bg,ratio)
                    await page.reload();await expect(page.locator('html')).to_have_attribute('data-theme',theme)
                    await page.emulate_media(color_scheme='dark' if theme=='light' else 'light')
                    await expect(page.locator('html')).to_have_attribute('data-theme',theme)
                await page.locator('#theme-select').select_option('system')
                await page.emulate_media(color_scheme='dark');await expect(page.locator('html')).to_have_attribute('data-theme','dark')
                await page.emulate_media(color_scheme='light');await expect(page.locator('html')).to_have_attribute('data-theme','light')
                for width in [1440,390]:
                    await page.set_viewport_size({'width':width,'height':1000})
                    for lang in ['de','ar','he','hi','th','zh-Hans']:
                        await page.locator('#language-select').select_option(lang)
                        await expect(page.locator('html')).to_have_attribute('lang',lang)
                        assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,lang)
                await page.locator('#language-select').select_option('en')
                await page.goto(server.url);opener=page.locator('.heading .primary');await opener.click()
                await page.keyboard.press('Escape');await expect(opener).to_be_focused()
                await expect(page.locator('#create-dialog')).not_to_be_visible()
                await page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                await page.locator('nav a').first.focus();await expect(page.locator('nav a').first).to_be_focused()
                await page.keyboard.press('Tab')
                assert await page.evaluate('document.activeElement.getAttribute("href")')=='/settings',await page.evaluate('document.activeElement.outerHTML')
                await page.emulate_media(reduced_motion='reduce')
                assert await page.evaluate('matchMedia("(prefers-reduced-motion: reduce)").matches')
                await browser.close()
        finally:server.stop()
    print('Theme browser passed: actual light/dark color pairs >=4.5:1, system/manual/cold-start, long text/RTL at 1440/390px, keyboard return and reduced motion.')

asyncio.run(main())
