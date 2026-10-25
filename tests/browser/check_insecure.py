"""Actual insecure browser origin with a constrained same-origin test proxy."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from support import ServerProcess

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading


def gateway(server):
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.connect(('192.0.2.1',80));address=sock.getsockname()[0];sock.close()
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.forward()
        def do_POST(self):self.forward()
        def forward(self):
            if self.headers.get('Host') != origin.split('//')[1]:self.send_error(403);return
            headers={k:v for k,v in self.headers.items() if k.lower() in ('content-type','idempotency-key','last-event-id')}
            if self.command=='POST':
                if self.headers.get('Origin') != origin:self.send_error(403);return
                headers['Origin']=server.url
            body=self.rfile.read(int(self.headers.get('Content-Length',0))) if self.command=='POST' else None
            req=urllib.request.Request(server.url+self.path,data=body,headers=headers,method=self.command)
            try:response=urllib.request.urlopen(req,timeout=15)
            except urllib.error.HTTPError as error:response=error
            self.send_response(response.status)
            for name in ['Content-Type','Content-Length','Cache-Control','Content-Security-Policy']:
                if response.headers.get(name):self.send_header(name,response.headers[name])
            self.end_headers()
            try:
                while chunk:=response.read1(65536):self.wfile.write(chunk);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            finally:response.close()
    proxy=ThreadingHTTPServer((address,0),Proxy);proxy.daemon_threads=True
    origin=f'http://{address}:{proxy.server_port}'
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    return proxy,origin


async def main():
    with tempfile.TemporaryDirectory() as directory:
        server=ServerProcess(directory).start()
        proxy,origin=gateway(server)
        try:
            async with async_playwright() as tool:
                browser=await tool.chromium.launch(channel='chromium',args=['--no-proxy-server'])
                context=await browser.new_context(locale='en-US')
                page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('dialog',lambda d:d.accept())
                await page.goto(origin)
                assert await page.evaluate('[isSecureContext,typeof crypto.randomUUID,typeof crypto.getRandomValues]')==[False,'undefined','function']
                for scenario in ['fail_once','slow']:
                    await page.goto(origin)
                    await page.get_by_role('button',name='+ New fixture',exact=True).click()
                    await page.get_by_label('Task name').fill('Insecure '+scenario)
                    await page.get_by_label('Scenario').select_option(scenario)
                    await page.get_by_label('Execution delay').fill('2500' if scenario=='slow' else '0')
                    await page.get_by_role('button',name='Create fixture →',exact=True).click()
                    await page.wait_for_url('**/runs/*');identity=page.url.rsplit('/',1)[1]
                    state=page.locator('.heading > .row-actions > .state')
                    if scenario=='slow':
                        await expect(state).to_have_text('RUNNING')
                        async with page.expect_response(lambda r:r.url.endswith('/cancel')) as response:
                            await page.get_by_role('button',name='Cancel',exact=True).click()
                        if (await response.value).status==409:
                            async with page.expect_response(lambda r:r.url.endswith('/cancel')) as response:
                                await page.get_by_role('button',name='Cancel',exact=True).click()
                        assert (await response.value).status in (200,202)
                        await expect(state).to_have_text('CANCELLED',timeout=15000)
                    else:
                        await expect(state).to_have_text('FAILED',timeout=15000)
                        await page.get_by_role('button',name='Retry',exact=True).click()
                        await expect(state).to_have_text('SUCCEEDED',timeout=15000)
                        await page.get_by_role('button',name='Add simulated target',exact=True).click()
                        target=page.locator('[data-intent-id]')
                        await target.locator('select').select_option('lost_response')
                        await target.get_by_role('button',name='Dispatch simulation',exact=True).click()
                        await expect(target.locator('.state')).to_have_text('UNKNOWN',timeout=15000)
                        await target.get_by_role('button',name='Check simulator receipt',exact=True).click()
                        await expect(target.locator('.state')).to_have_text('PUBLISHED')
                        run=server.request('/v1/runs/'+identity)[1]
                        assert len(run['attempts'])==2 and len(run['publications'][0]['attempts'])==1
                assert not errors,errors
                # Separate negative-capability context; not used for the insecure-origin proof.
                negative=await browser.new_context(locale='en-US')
                await negative.add_init_script("Object.defineProperty(window,'crypto',{value:undefined});")
                p=await negative.new_page();await p.goto(server.url)
                await p.get_by_role('button',name='+ New fixture',exact=True).click()
                await p.get_by_label('Task name').fill('Cannot send')
                before=server.request('/v1/runs?limit=100')[1]
                await p.get_by_role('button',name='Create fixture →',exact=True).click()
                await expect(p.locator('#form-error')).to_contain_text('Secure randomness')
                assert server.request('/v1/runs?limit=100')[1]==before
                await browser.close()
        finally:proxy.shutdown();proxy.server_close();server.stop()
    print('Insecure-origin browser passed: native crypto fallback, create/cancel/retry/target/dispatch/query; visible missing-randomness failure.')

asyncio.run(main())
