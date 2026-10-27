"""Real Chromium/storage and the production Linux host; no Electron claim."""
import asyncio
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
NODE = os.environ['LOOPCAST_DESKTOP_NODE']

def start(filename):
    reader, writer = os.pipe()
    process = subprocess.Popen([NODE, str(ROOT/'desktop/scripts/run-local.mjs'), str(filename), str(writer)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(writer,), env={})
    os.close(writer)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(reader, selectors.EVENT_READ)
            if not selector.select(20): raise AssertionError('Managed browser composition did not start')
        with os.fdopen(reader, 'rb') as stream: bootstrap = json.loads(stream.readline())
        return process, bootstrap
    except BaseException:
        process.kill(); process.wait(); raise

def stop(process):
    process.stdin.write(b'STOP\n'); process.stdin.close()
    assert process.wait(20) == 0

async def main():
    with tempfile.TemporaryDirectory(prefix='loopcast-desktop-browser-') as work:
        root = Path(work); data = root/'data'; session = root/'session'
        data.mkdir(mode=0o700); session.mkdir(mode=0o700)
        args = ['--python', os.environ['LOOPCAST_DESKTOP_PYTHON'], '--node', NODE, '--next', os.environ['LOOPCAST_DESKTOP_NEXT'], '--data', str(data), '--session', str(session), '--port', '28787', '--revision', os.environ['LOOPCAST_DESKTOP_REVISION']]
        plan = subprocess.check_output([NODE, str(ROOT/'desktop/scripts/prepare.mjs'), *args], env={})
        manifest = root/'desktop.json'; manifest.write_bytes(plan)
        process, bootstrap = start(manifest)
        try:
            async with async_playwright() as tool:
                profile = root/'chromium-profile'; captured = []; committed = None
                async def window():
                    context = await tool.chromium.launch_persistent_context(str(profile), channel='chromium', headless=True, locale='en-US')
                    async def capability(route):
                        # Test driver only: exact origin routing; Electron's native
                        # frame/initiator behavior remains a separate acceptance.
                        await route.continue_(headers={**route.request.headers, 'X-LoopCast-Desktop-Session': bootstrap['gatewayToken']})
                    await context.route(bootstrap['origin']+'/**', capability)
                    return context, context.pages[0]
                context, page = await window()
                async def response_loss(route):
                    nonlocal committed
                    captured.append(route.request.headers['idempotency-key'])
                    response = await route.fetch(headers={**route.request.headers, 'X-LoopCast-Desktop-Session': bootstrap['gatewayToken']})
                    committed = await response.json(); await route.abort()
                await page.route('**/v1/runs', response_loss)
                await page.goto(bootstrap['origin']); await page.locator('.heading .primary').click()
                await page.locator('input[name=title]').fill('Lost response across host restart')
                await page.locator('#create-form button[type=submit]').click()
                await expect(page.locator('#form-error')).not_to_be_empty()
                assert len(captured) == 1 and committed['run_id']
                original_cookie = await context.cookies()
                devtools = await context.new_cdp_session(page); await devtools.send('Network.clearBrowserCache')
                await context.close(); stop(process)
                process, bootstrap = start(manifest)
                context, page = await window()
                async def retry(route):
                    captured.append(route.request.headers['idempotency-key'])
                    response = await route.fetch(headers={**route.request.headers, 'X-LoopCast-Desktop-Session': bootstrap['gatewayToken']})
                    await route.fulfill(response=response)
                await page.route('**/v1/runs', retry)
                await page.goto(bootstrap['origin'])
                await expect(page.locator('#create-dialog')).to_be_visible()
                await expect(page.locator('input[name=title]')).to_have_value('Lost response across host restart')
                assert len(captured) == 1  # Reload/restart never automatically writes.
                before = await context.cookies()
                assert [c['value'] for c in before if c['name']=='loopcast_owner'] == [c['value'] for c in original_cookie if c['name']=='loopcast_owner']
                await page.locator('#create-form button[type=submit]').click()
                await page.wait_for_url('**/runs/'+committed['run_id'])
                assert len(captured) == 2 and captured[0] == captured[1]
                assert await page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('loopcast.run.create.pending.v1.')).length") == 0
                await context.close()
        finally:
            if process.poll() is None: stop(process)
    print('Real Chromium + production host restart passed: committed lost response, stable owner cookie, cache-only clear, durable pending identity, explicit same-key retry, no automatic second write. Electron NOT_RUN.')

if __name__ == '__main__': asyncio.run(main())
