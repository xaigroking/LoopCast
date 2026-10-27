// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { secureWindow, sameOrigin, webPreferences } from '../src/window-policy.mjs';

function fixture() {
  const origin = 'http://127.0.0.1:12345', token = 'fixture-session';
  const contents = Object.assign(new EventEmitter(), { id: 7, mainFrame: { processId: 12, routingId: 34, frames: [] }, getURL: () => origin,
    setWindowOpenHandler(handler) { this.open = handler; } });
  const hooks = {}, session = Object.assign(new EventEmitter(), {
    webRequest: { onBeforeSendHeaders(filter, handler) { hooks.send = handler; hooks.filter = filter; }, onHeadersReceived(_filter, handler) { hooks.headers = handler; } },
    setPermissionRequestHandler(handler) { hooks.permission = handler; }, setPermissionCheckHandler(handler) { hooks.check = handler; },
  });
  secureWindow({ webContents: contents }, session, origin, token);
  return { origin, token, contents, session, hooks };
}
test('isolated window has no Node integration, preload or permissive web security', () => {
  assert.equal(webPreferences.sandbox, true); assert.equal(webPreferences.contextIsolation, true);
  assert.equal(webPreferences.webSecurity, true); assert.equal(webPreferences.nodeIntegration, false);
  assert.equal(webPreferences.preload, undefined);
});
test('Electron event objects deny frames, navigation, redirects, new windows and permissions', () => {
  const { contents, session, hooks, origin } = fixture();
  assert.deepEqual(contents.open(), { action: 'deny' });
  for (const [name, extra] of [
    ['will-navigate', { url: 'https://example.com' }],
    ['will-frame-navigate', { url: origin, isMainFrame: false }],
    ['will-frame-navigate', { url: 'file:///tmp/page', isMainFrame: true }],
    ['will-redirect', { url: 'http://127.0.0.1:54321', isMainFrame: true }],
    ['will-attach-webview', {}],
  ]) {
    let prevented = false; contents.emit(name, { ...extra, preventDefault() { prevented = true; } });
    assert.equal(prevented, true, name);
  }
  let allowed = true; hooks.permission(null, 'camera', value => { allowed = value; });
  assert.equal(allowed, false); assert.equal(hooks.check(), false);
  let download = false; session.emit('will-download', { preventDefault() { download = true; } }); assert.equal(download, true);
});
test('capability is only injected for this main frame and exact origin', () => {
  const { contents, hooks, origin, token } = fixture();
  const base = { webContentsId: contents.id, url: `${origin}/v1/runs`, resourceType: 'xhr',
    frame: contents.mainFrame, initiatorOrigin: origin, requestHeaders: { 'x-loopcast-desktop-session': 'forged' } };
  let result;
  hooks.send(base, value => { result = value; });
  assert.equal(result.cancel, false); assert.equal(result.requestHeaders['X-LoopCast-Desktop-Session'], token);
  assert.equal(result.requestHeaders['x-loopcast-desktop-session'], undefined);
  for (const change of [{ webContentsId: 8 }, { resourceType: 'subFrame' }, { frame: { processId: 12, routingId: 35 } }, { frame: null }, { initiatorOrigin: 'null' }, { url: 'http://127.0.0.1:54321/v1' }]) {
    hooks.send({ ...base, ...change }, value => { result = value; });
    assert.equal(result.cancel, true); assert.equal(result.requestHeaders['X-LoopCast-Desktop-Session'], undefined);
  }
  assert.deepEqual(hooks.filter.urls, [`${origin}/*`]);
  assert.equal(sameOrigin('http://user@127.0.0.1:12345', origin), false);
});
test('window response policy preserves Next nonce and offline resources', () => {
  const { hooks } = fixture(); let result;
  hooks.headers({ responseHeaders: { 'Content-Security-Policy': ["script-src 'self' 'nonce-test'; font-src 'self'; media-src 'self'"] } }, value => { result = value; });
  assert.equal(result.responseHeaders['Content-Security-Policy'][0], "script-src 'self' 'nonce-test'; font-src 'self'; media-src 'self'; frame-src 'none'");
});

test('blank child creation permanently revokes authority and disposed frames fail closed', () => {
  const { contents, hooks, origin } = fixture();
  const request = { webContentsId: contents.id, url: `${origin}/healthz`, resourceType: 'xhr',
    frame: contents.mainFrame, initiatorOrigin: origin, requestHeaders: {} };
  let result;
  contents.emit('frame-created', {}, { frame: { parent: contents.mainFrame } });
  hooks.send(request, value => { result = value; });
  assert.equal(result.cancel, true); assert.equal(Object.keys(result.requestHeaders).length, 0);
  const disposed = fixture();
  Object.defineProperty(disposed.contents, 'mainFrame', { get() { throw new Error('disposed native object'); } });
  disposed.hooks.send(request, value => { result = value; });
  assert.equal(result.cancel, true); assert.equal(Object.keys(result.requestHeaders).length, 0);
});
