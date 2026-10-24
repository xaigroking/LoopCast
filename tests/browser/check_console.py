"""Browser acceptance against a separately installed application and real HTTP API."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
from support import ServerProcess

STATE = '.heading > .row-actions > .state'


async def create(page, title, scenario='success', delay=300):
    await page.goto(page.base_url)
    await page.get_by_role('button', name='+ New fixture', exact=True).click()
    await page.get_by_label('Task name').fill(title)
    await page.get_by_label('Scenario').select_option(scenario)
    await page.get_by_label('Execution delay').fill(str(delay))
    await page.get_by_role('button', name='Create fixture →', exact=True).click()
    await page.wait_for_url('**/runs/*')
    return page.url.rsplit('/', 1)[1]


async def main():
    errors = []
    with tempfile.TemporaryDirectory(prefix='loopcast-browser-') as directory:
        server = ServerProcess(directory).start()
        try:
            async with async_playwright() as tool:
                browser = await tool.chromium.launch(channel="chromium")
                context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                await context.add_init_script('''
                    window.activeStreams = 0;
                    const Native = window.EventSource;
                    window.EventSource = class extends Native {
                      constructor(...args) { super(...args); window.activeStreams++; this.counted = true; }
                      close() { if(this.counted) { window.activeStreams--; this.counted = false; } super.close(); }
                    };
                ''')
                page = await context.new_page()
                page.base_url = server.url
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                await page.goto(server.url)
                await expect(page.get_by_text('A fresh start.', exact=True)).to_be_visible()
                await expect(page.get_by_text('FIXTURE MODE', exact=True)).to_be_visible()
                title = '<img src=x onerror=alert(1)> browser fixture'
                success = await create(page, title)
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED', timeout=15000)
                await expect(page.get_by_role('heading', name=title)).to_be_visible()
                assert await page.locator('main img').count() == 0
                report_link = page.get_by_role('link', name='Open fixture report', exact=False)
                await expect(report_link).to_be_visible()
                async with page.expect_popup() as event:
                    await report_link.click()
                popup = await event.value
                await expect(popup.locator('body')).to_contain_text('LoopCast fixture report')
                await popup.close()
                data = server.request('/v1/runs/' + success)[1]
                report = server.request('/v1/artifacts/' + data['artifacts'][0]['id'] + '/content')[1]
                assert hashlib.sha256(report).hexdigest() == data['artifacts'][0]['sha256']
                await expect(page.get_by_role('button', name='Retry', exact=True)).to_be_disabled()
                assert await page.evaluate('window.activeStreams') == 0

                # The server accepts a create, but the first network response is lost.
                await page.goto(server.url)
                seen = []
                async def lose_first_response(route):
                    if route.request.method != 'POST':
                        return await route.continue_()
                    seen.append(route.request.headers.get('idempotency-key'))
                    response = await route.fetch()
                    if len(seen) == 1:
                        await route.abort('failed')
                    else:
                        await route.fulfill(response=response)
                await page.route('**/v1/runs', lose_first_response)
                await page.get_by_role('button', name='+ New fixture', exact=True).click()
                await page.get_by_label('Task name').fill('Lost response fixture')
                await page.get_by_role('button', name='Create fixture →', exact=True).click()
                await expect(page.locator('#form-error')).to_contain_text('retry safely')
                await page.get_by_role('button', name='Create fixture →', exact=True).click()
                await page.wait_for_url('**/runs/*')
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED', timeout=15000)
                await page.unroute('**/v1/runs', lose_first_response)
                assert len(seen) == 2 and seen[0] == seen[1]
                matching = [r for r in server.request('/v1/runs?limit=100')[1]['items'] if r['input']['title'] == 'Lost response fixture']
                assert len(matching) == 1

                # Narrow-screen retry uses the same backend command policy.
                await page.set_viewport_size({'width': 390, 'height': 844})
                failed = await create(page, 'Retry from narrow screen', 'fail_once', 150)
                await expect(page.locator(STATE)).to_have_text('FAILED', timeout=15000)
                old_failed = server.request('/v1/runs/' + failed)[1]
                await page.get_by_role('button', name='Retry', exact=True).click()
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED', timeout=15000)
                current = server.request('/v1/runs/' + failed)[1]
                assert len(current['attempts']) == 2
                assert current['attempts'][0] == old_failed['attempts'][0]
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.screenshot(path='/tmp/loopcast-console-mobile.png', full_page=True)

                # Deliver a stale HTTP snapshot after the terminal version is already known.
                async def stale(route):
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps(old_failed))
                await page.route('**/v1/runs/' + failed, stale)
                await page.get_by_role('button', name='Refresh', exact=True).click()
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED')
                await expect(page.get_by_text('Attempt 2 · State version', exact=False)).to_be_visible()
                await page.unroute('**/v1/runs/' + failed, stale)

                # Old/duplicate SSE payloads cannot overwrite a newer version.
                slow = await create(page, 'Reconnect and cancel', 'slow', 2500)
                initial = server.request('/v1/runs/' + slow)[1]
                await expect(page.locator(STATE)).to_have_text('RUNNING', timeout=15000)
                await context.set_offline(True)
                await expect(page.locator('#sync')).to_contain_text('Offline')
                await context.set_offline(False)
                await expect(page.get_by_role('heading', name='Reconnect and cancel')).to_be_visible()
                await page.goto(server.url)
                card = page.locator(f'[data-run-id="{slow}"]')
                await card.locator('summary').click()
                await card.locator('.menu').get_by_role('button', name='Cancel', exact=True).click()
                await page.goto(server.url + '/runs/' + slow)
                await expect(page.locator(STATE)).to_have_text('CANCELLED', timeout=15000)
                cancelled = server.request('/v1/runs/' + slow)[1]
                assert len(cancelled['attempts']) == 1
                assert not cancelled['allowed_actions']['retry']['enabled']
                # Inject genuine historical snapshots, never invented success facts.
                async def old_events(route):
                    payload = f"id: {cancelled['last_event_id']}\nevent: snapshot\ndata: {json.dumps(cancelled)}\n\n"
                    payload += f"id: {initial['last_event_id']}\nevent: snapshot\ndata: {json.dumps(initial)}\n\n" * 2
                    payload += f"id: {cancelled['last_event_id']}\nevent: complete\ndata: {json.dumps(cancelled)}\n\n"
                    await route.fulfill(status=200, content_type='text/event-stream', body=payload)
                await page.route('**/v1/runs/' + slow, lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(initial)))
                await page.route('**/v1/runs/' + slow + '/events?*', old_events)
                await page.reload()
                await expect(page.locator(STATE)).to_have_text('CANCELLED', timeout=15000)
                await page.unroute_all()
                assert await page.evaluate('window.activeStreams') == 0

                before = server.request('/v1/runs?limit=100')[1]['items']
                for n in range(30):
                    await page.goto(server.url + '/runs/' + (success if n % 2 else failed))
                    await expect(page.locator(STATE)).to_have_text('SUCCEEDED')
                    await page.reload()
                    await expect(page.locator(STATE)).to_have_text('SUCCEEDED')
                    assert await page.evaluate('window.activeStreams') == 0
                after = server.request('/v1/runs?limit=100')[1]['items']
                assert [(r['run_id'], len(r['attempts'])) for r in before] == [(r['run_id'], len(r['attempts'])) for r in after]
                await page.goto(server.url)
                await page.get_by_role('link', name=title, exact=True).click()
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED')
                await page.go_back()
                await expect(page.get_by_role('heading', name='Your task console')).to_be_visible()
                await page.go_forward()
                await expect(page.locator(STATE)).to_have_text('SUCCEEDED')
                await page.goto(server.url + '/runs/' + '0' * 32)
                await expect(page.get_by_role('heading', name='Task or address not found')).to_be_visible()
                assert server.request('/v1/assets/missing')[0] == 404
                await page.set_viewport_size({'width': 1440, 'height': 1000})
                await page.goto(server.url)
                await expect(page.get_by_role('heading', name='Your task console')).to_be_visible()
                await page.screenshot(path='/tmp/loopcast-console-desktop.png', full_page=True)
                assert not errors, errors
                await browser.close()
            print('Browser acceptance passed: installed assets, desktop/mobile, create, retry, menu cancel, report, offline recovery, stale HTTP/SSE, 30 refresh/switch cycles, history, missing object, subscription cleanup.')
        finally:
            server.stop()


if __name__ == '__main__':
    asyncio.run(main())
