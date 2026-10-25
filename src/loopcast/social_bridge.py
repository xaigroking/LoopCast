"""Standalone child protocol for a separately provisioned SAU environment.

This file deliberately imports no LoopCast application, database or HTTP server.
It returns safe identities/results; cookies and temporary controls stay private.
"""
import asyncio
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import urlsplit

PAGES = []
KEYS = {'Enter', 'Tab', 'Backspace', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Delete', 'Home', 'End', 'Space'}
HOME = {'douyin': 'https://creator.douyin.com/', 'xiaohongshu': 'https://creator.xiaohongshu.com/', 'bilibili': 'https://passport.bilibili.com/login', 'youtube': 'https://studio.youtube.com/'}


def atomic_json(path, value):
    temp = path.with_suffix('.pending')
    temp.write_text(json.dumps(value)); temp.chmod(0o600); temp.replace(path)


def take_controls(work):
    directory = work / 'controls'
    values = []
    if directory.exists():
        for path in directory.glob('*.json'):
            try: values.append((json.loads(path.read_text()), path))
            except (OSError, ValueError): continue
    for value, path in sorted(values, key=lambda pair: pair[0].get('sequence', 0)):
        path.unlink(missing_ok=True)
        (work / ('done-' + path.name)).touch(mode=0o600)
        yield value


async def browser_controls(work):
    while True:
        active = next((page for page in reversed(PAGES) if not page.is_closed()), None)
        if active:
            try:
                await active.screenshot(path=str(work / 'frame.pending.png'), mask=[active.locator('input[type=password],input[autocomplete=current-password],input[name*=password]')], timeout=3000)
                (work / 'frame.pending.png').replace(work / 'frame.png')
                (work / 'frame.png').chmod(0o600)
                for value in take_controls(work):
                    kind = value.get('kind')
                    if kind == 'click' and type(value.get('x')) in (int, float) and type(value.get('y')) in (int, float):
                        await active.mouse.click(max(0, min(1280, value['x'])), max(0, min(800, value['y'])))
                    elif kind == 'text' and isinstance(value.get('text'), str): await active.keyboard.insert_text(value['text'][:1000])
                    elif kind == 'key' and value.get('text') in KEYS: await active.keyboard.press(value['text'])
                    elif kind == 'refresh': await active.reload(wait_until='domcontentloaded', timeout=15000)
            except Exception:
                pass
        await asyncio.sleep(.3)


def configure_browser(settings):
    from playwright.async_api import BrowserType, Browser
    launch = BrowserType.launch
    new_context = Browser.new_context
    async def controlled_launch(self, *args, **kwargs):
        kwargs['headless'] = True
        kwargs['executable_path'] = settings['browser_path']
        kwargs.pop('channel', None)
        if settings.get('proxy_url'):
            from urllib.parse import unquote
            proxy = urlsplit(settings['proxy_url'])
            host = '[' + proxy.hostname + ']' if ':' in proxy.hostname else proxy.hostname
            kwargs['proxy'] = {'server': proxy.scheme + '://' + host + (':' + str(proxy.port) if proxy.port else '')}
            if proxy.username is not None: kwargs['proxy']['username'] = unquote(proxy.username)
            if proxy.password is not None: kwargs['proxy']['password'] = unquote(proxy.password)
        return await launch(self, *args, **kwargs)
    async def controlled_context(self, *args, **kwargs):
        kwargs['viewport'] = {'width': 1280, 'height': 800}
        context = await new_context(self, *args, **kwargs)
        context.on('page', lambda page: PAGES.append(page))
        return context
    BrowserType.launch, Browser.new_context = controlled_launch, controlled_context


def value_at(value, path):
    for key in path:
        if not isinstance(value, dict): raise ValueError('Invalid identity response')
        value = value.get(key)
    return value


async def verified_profile(platform, credential, settings):
    from playwright.async_api import async_playwright
    state = json.loads(credential.read_text())
    if platform == 'bilibili':
        state = {'cookies': [{'name': c['name'], 'value': c['value'], 'domain': '.bilibili.com', 'path': '/'} for c in state['cookie_info']['cookies']], 'origins': []}
    async with async_playwright() as tool:
        browser = await tool.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=state)
            page = await context.new_page()
            if platform == 'youtube':
                await page.goto(HOME[platform], wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_url(re.compile(r'https://studio\.youtube\.com/channel/UC[A-Za-z0-9_-]+.*'), timeout=20000)
                subject = re.search(r'/channel/(UC[A-Za-z0-9_-]+)', page.url).group(1)
                return {'stable_subject': subject, 'label': subject}
            if platform == 'bilibili':
                response = await context.request.get('https://api.bilibili.com/x/space/myinfo', timeout=15000, max_redirects=0)
                data = await response.json()
                if response.status != 200 or data.get('code') != 0 or not data.get('data', {}).get('mid'): raise ValueError('Identity was not verified')
                return {'stable_subject': str(data['data']['mid']), 'label': str(data['data'].get('name') or data['data']['mid'])[:120]}
            probe = settings['identity_probe']
            if not probe: raise ValueError('Own-account identity probe is required')
            expected = urlsplit(probe['url'])
            # Observe the site's own authenticated profile request. This preserves
            # platform signing and never accepts a subject supplied by the user.
            async with page.expect_response(lambda r: urlsplit(r.url).scheme == 'https' and urlsplit(r.url).hostname == expected.hostname and urlsplit(r.url).path == expected.path, timeout=30000) as observed:
                await page.goto(HOME[platform], wait_until='domcontentloaded', timeout=30000)
            response = await observed.value
            value = await response.json()
            success = value_at(value, probe['success_path'])
            if response.status != 200 or type(success) is not type(probe['success_value']) or success != probe['success_value']:
                raise ValueError('Identity was not verified')
            raw_subject = value_at(value, probe['id_path'])
            if type(raw_subject) not in (str, int): raise ValueError('Identity was not verified')
            subject = str(raw_subject)
            if not re.fullmatch(r'[A-Za-z0-9_:.@-]{1,128}', subject) or subject in {'None', 'null', '0'}:
                raise ValueError('Identity was not verified')
            return {'stable_subject': subject, 'label': str(value_at(value, probe['label_path']) or subject)[:120]}
        finally:
            await browser.close()


async def bili_login(work, credential, settings):
    import pty
    import subprocess
    import select
    master, slave = pty.openpty()
    process = subprocess.Popen([settings['biliup'], '-u', str(credential), 'login'], cwd=work, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    # A private PTY makes the upstream's actual interactive QR login reachable.
    # Only QR images and fixed prompt codes are exposed, never terminal output.
    os.set_blocking(master, False)
    tail = ''
    try:
        while process.poll() is None:
            if select.select([master], [], [], 0)[0]:
                try: tail = (tail + os.read(master, 65536).decode(errors='ignore'))[-8000:]
                except OSError: pass
            image = work / 'qrcode.png'
            if image.is_file() and not image.is_symlink() and image.stat().st_size < 1048576:
                data = image.read_bytes()
                if data.startswith(b'\x89PNG\r\n\x1a\n'):
                    (work / 'frame.pending.png').write_bytes(data); (work / 'frame.pending.png').replace(work / 'frame.png')
            for command in take_controls(work):
                if command.get('kind') == 'confirm': os.write(master, b'\r')
                if command.get('kind') == 'key':
                    key = {'Enter': b'\r', 'ArrowUp': b'\x1b[A', 'ArrowDown': b'\x1b[B', 'Escape': b'\x1b'}.get(command.get('text'))
                    if key: os.write(master, key)
            await asyncio.sleep(.1)
        if process.returncode or not credential.is_file(): raise RuntimeError('Authorization failed')
    finally:
        if process.poll() is None: process.kill(); process.wait()
        os.close(master)


async def authorize(platform, work, settings):
    credential = work / 'credentials.json'
    atomic_json(work / 'progress.json', {'state': 'WAITING'})
    if platform == 'bilibili':
        await bili_login(work, credential, settings)
    else:
        module = importlib.import_module('uploader.' + {'douyin': 'douyin_uploader', 'xiaohongshu': 'xiaohongshu_uploader', 'youtube': 'youtube_uploader'}[platform] + '.main')
        function = getattr(module, platform + '_setup' if platform != 'youtube' else 'youtube_setup')
        # BrowserType.launch is controlled above, including upstream functions
        # which would otherwise insist on an inaccessible headed window.
        result = await function(str(credential), handle=True, return_detail=True, headless=True)
        if not isinstance(result, dict) or result.get('success') is not True: raise RuntimeError('Authorization failed')
    atomic_json(work / 'progress.json', {'state': 'VERIFYING'})
    profile = await verified_profile(platform, credential, settings)
    return {'profile': profile}


async def upload(platform, payload, settings):
    module = importlib.import_module('sau_commands.' + platform)
    credential = Path(payload['credential_file'])
    module.resolve_account_file = lambda *_: credential
    account_name = 'controlled'
    common = {'account_name': account_name, 'video_file': Path(payload['video_file']), 'title': payload['title'], 'description': payload['description'], 'tags': []}
    cover = Path(payload['cover_file']) if payload.get('cover_file') else None
    if platform == 'douyin':
        request = module.DouyinVideoUploadRequest(**common, publish_date=0, thumbnail_file=cover, headless=True, debug=False, declaration=payload['fields'].get('declaration'))
        await module.upload_video(request)
    elif platform == 'xiaohongshu':
        request = module.XiaohongshuVideoUploadRequest(**common, publish_date=0, thumbnail_file=cover, headless=True, debug=False)
        await module.upload_xiaohongshu_video(request)
    elif platform == 'youtube':
        request = module.YouTubeVideoUploadRequest(**common, thumbnail_file=cover, visibility=payload['fields']['visibility'], headless=True, debug=False)
        await module.upload_youtube_video(request)
    else:
        runtime = importlib.import_module('uploader.bilibili_uploader.runtime')
        runtime.ensure_biliup_binary = lambda *args, **kwargs: Path(settings['biliup'])
        request = module.BilibiliVideoUploadRequest(**common, tid=payload['fields']['category'], publish_date=0, thumbnail_file=cover)
        await module.upload_bilibili_video(request)
    # All four pinned command adapters return credential paths, not confirmed
    # work receipts. A successful command is recorded as submission only.
    return {'outcome': 'SUBMITTED'}


async def execute(request, work):
    settings, platform, operation = request['settings'], request['platform'], request['operation']
    if platform not in HOME or operation not in {'authorize', 'check', 'upload'}: raise ValueError('Unsupported operation')
    sys.path.insert(0, settings['source_root'])
    import conf
    conf.BASE_DIR = work
    configure_browser(settings)
    controls = asyncio.create_task(browser_controls(work)) if operation == 'authorize' and platform != 'bilibili' else None
    try:
        if operation == 'authorize': return await authorize(platform, work, settings)
        if operation == 'check':
            try:
                profile = await verified_profile(platform, work / 'credentials.json', settings)
                return {'valid': True, 'profile': profile}
            except Exception: return {'valid': False}
        return await upload(platform, request['payload'], settings)
    finally:
        if controls:
            controls.cancel()
            with contextlib.suppress(asyncio.CancelledError): await controls


def main():
    path = Path(sys.argv[1]).resolve()
    request = json.loads(path.read_text())
    # Upstream prints/logs may contain cookie filenames or raw service responses.
    # They stay out of the parent protocol and diagnostic stream.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        result = asyncio.run(execute(request, path.parent))
    print(json.dumps(result))


if __name__ == '__main__':
    try: main()
    except BaseException:
        sys.exit(2)
