// SPDX-License-Identifier: Apache-2.0
export const webPreferences = Object.freeze({
  contextIsolation: true, sandbox: true, nodeIntegration: false,
  webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
});

export function sameOrigin(url, origin) {
  try { const parsed = new URL(url); return parsed.origin === origin && !parsed.username && !parsed.password; }
  catch { return false; }
}

/**
 * @param {import('electron').BrowserWindow} window
 * @param {import('electron').Session} session
 * @param {string} origin
 * @param {string} token
 */
export function secureWindow(window, session, origin, token) {
  const contents = window.webContents;
  session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
  session.on('will-download', event => event.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.on('will-navigate', event => { if (!sameOrigin(event.url, origin)) event.preventDefault(); });
  contents.on('will-frame-navigate', event => {
    if (!event.isMainFrame || !sameOrigin(event.url, origin)) event.preventDefault();
  });
  contents.on('will-redirect', event => {
    if (!event.isMainFrame || !sameOrigin(event.url, origin)) event.preventDefault();
  });
  session.webRequest.onBeforeSendHeaders({ urls: [`${origin}/*`] }, (details, callback) => {
    const headers = { ...details.requestHeaders };
    for (const key of Object.keys(headers)) if (key.toLowerCase() === 'x-loopcast-desktop-session') delete headers[key];
    const mainFrame = details.frame && details.frame.processId === contents.mainFrame.processId && details.frame.routingId === contents.mainFrame.routingId;
    const trusted = details.webContentsId === contents.id && details.resourceType !== 'subFrame' && sameOrigin(details.url, origin) &&
      (details.resourceType === 'mainFrame' || (mainFrame && sameOrigin(contents.getURL(), origin) && details.initiatorOrigin === origin));
    if (trusted) headers['X-LoopCast-Desktop-Session'] = token;
    callback({ requestHeaders: headers, cancel: !trusted });
  });
  // The existing Next nonce CSP governs scripts, media, fonts and connections.
  // Add frame-src without replacing its per-response nonce.
  session.webRequest.onHeadersReceived({ urls: [`${origin}/*`] }, (details, callback) => {
    const headers = { ...details.responseHeaders };
    const key = Object.keys(headers).find(k => k.toLowerCase() === 'content-security-policy');
    if (key) headers[key] = headers[key].map(value => `${value}; frame-src 'none'`);
    callback({ responseHeaders: headers });
  });
}
