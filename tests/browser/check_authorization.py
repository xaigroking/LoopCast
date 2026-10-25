"""Actual pinned-browser controls over an installed bridge, with local HTML only."""
import asyncio
import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from playwright.async_api import async_playwright


async def until(predicate):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if await predicate(): return
        await asyncio.sleep(.05)
    raise AssertionError('Local browser control did not settle')


async def main():
    spec=importlib.util.spec_from_file_location('installed_bridge',os.environ['LOOPCAST_INSTALLED_BRIDGE'])
    bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
    with tempfile.TemporaryDirectory() as directory:
        work=Path(directory);(work/'controls').mkdir()
        async with async_playwright() as tool:
            bridge.configure_browser({'browser_path':tool.chromium.executable_path})
            browser=await tool.chromium.launch()
            context=await browser.new_context();page=await context.new_page()
            await page.set_content('<label>Private<input id="private" type="password" value="screen-password-canary"></label><label>Public<input id="public"></label>')
            bounds=await page.locator('#private').bounding_box()
            task=asyncio.create_task(bridge.browser_controls(work))
            try:
                async def has_frame():return (work/'frame.png').is_file()
                await until(has_frame)
                data=base64.b64encode((work/'frame.png').read_bytes()).decode()
                pixel=await page.evaluate('''async value => { const image=new Image();image.src='data:image/png;base64,'+value.data;await image.decode();const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);return [...ctx.getImageData(value.x,value.y,1,1).data]; }''',{'data':data,'x':int(bounds['x']+10),'y':int(bounds['y']+10)})
                assert pixel==[255,0,255,255],pixel
                public=await page.locator('#public').bounding_box()
                bridge.atomic_json(work/'controls/1.json',{'sequence':1,'kind':'click','x':public['x']+10,'y':public['y']+10})
                bridge.atomic_json(work/'controls/2.json',{'sequence':2,'kind':'text','text':'ephemeral-input-canary'})
                async def text_received():return await page.locator('#public').input_value()=='ephemeral-input-canary'
                await until(text_received)
                bridge.atomic_json(work/'controls/3.json',{'sequence':3,'kind':'key','text':'Backspace'})
                async def key_received():return await page.locator('#public').input_value()=='ephemeral-input-canar'
                await until(key_received)
                assert not list((work/'controls').glob('*.json'))
                assert all(path.read_bytes()==b'' for path in work.glob('done-*'))
                assert (work/'frame.png').stat().st_mode & 0o777==0o600
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):await task
                await browser.close()
    print('Installed authorization bridge passed: real headless browser screen, password mask, pointer/text/fixed-key controls, consumed private payloads and file permissions; no platform login')

if __name__=='__main__':asyncio.run(main())
