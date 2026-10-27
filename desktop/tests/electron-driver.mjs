// SPDX-License-Identifier: Apache-2.0
// Actual executable + native Node inspector and browser CDP. No Electron loader
// replaces app.ready, app.isReady, secureWindow or capability headers.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { chromium } from 'playwright-core';
import { Budget } from '../src/errors.mjs';

export async function launchElectron({ executable, directory, manifest, env, observe = true, trace }) {
  const args = [observe ? '--inspect-brk=0' : '--inspect=0', '--remote-debugging-port=0', directory, '--desktop-config', manifest];
  const child = spawn(executable, args, { env, stdio: ['ignore', 'ignore', 'pipe'] });
  let status, inspectorURL, browserURL, pending = '', diagnostic = '', debuggerWaiting = false;
  const exited = new Promise(resolve => {
    child.once('exit', (code, signal) => { status = { code, signal }; resolve(status); });
    child.once('error', () => { status = { code: null, signal: null }; resolve(status); });
  });
  child.stderr.on('data', chunk => {
    pending += chunk;
    while (pending.includes('\n')) {
      const index = pending.indexOf('\n'), line = pending.slice(0, index); pending = pending.slice(index + 1);
      const node = /^Debugger listening on (ws:\/\/127\.0\.0\.1:[0-9]+\/[^ ]+)/.exec(line);
      const browser = /^DevTools listening on (ws:\/\/127\.0\.0\.1:[0-9]+\/[^ ]+)/.exec(line);
      if (node) inspectorURL = node[1];
      else if (browser) browserURL = browser[1];
      else if (line.includes('Waiting for the debugger to disconnect')) debuggerWaiting = true;
      // Keep diagnostic categories only. Debugger URLs, headers, full environment
      // and application output never become logs or test attachments.
      else if (/sandbox|namespace|shared libraries/i.test(line)) diagnostic = 'RUNTIME_SANDBOX_OR_LIBRARY_FAILURE';
    }
    if (pending.length > 4096) pending = '';
  });
  const budget = new Budget(45000);
  async function until(fn) {
    while (!fn()) {
      if (status) throw new Error(diagnostic || 'ELECTRON_EXITED_BEFORE_READY');
      await budget.run(new Promise(resolve => setTimeout(resolve, 20)));
    }
    return fn();
  }
  let socket, browser;
  try {
    await until(() => inspectorURL);
    socket = new WebSocket(inspectorURL); await budget.run(once(socket, 'open'));
    let serial = 0, entryBreakpoint, entryPaused;
    const entry = new Promise(resolve => { entryPaused = resolve; });
    const requests = new Map();
    socket.addEventListener('message', event => {
      const result = JSON.parse(event.data);
      if (result.id) { const task = requests.get(result.id); requests.delete(result.id); task?.resolve(result); }
      if (result.method === 'Debugger.paused') {
        if (entryBreakpoint && result.params.hitBreakpoints?.includes(entryBreakpoint)) entryPaused();
        else socket.send(JSON.stringify({ id: ++serial, method: 'Debugger.resume' }));
      }
    });
    socket.addEventListener('close', () => {
      for (const task of requests.values()) task.resolve({ error: true });
      requests.clear();
    });
    async function send(method, params = {}) {
      const id = ++serial;
      const result = await new Budget(10000).run(new Promise(resolve => { requests.set(id, { resolve }); socket.send(JSON.stringify({ id, method, params })); }));
      if (result.error) throw new Error('NODE_INSPECTOR_COMMAND_FAILED');
      return result.result;
    }
    async function evaluate(fn, argument, awaitPromise = true) {
      const expression = `(()=>{const electron=require('electron');return (${fn.toString()})(electron,${JSON.stringify(argument) ?? 'undefined'});})()`;
      const result = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true, includeCommandLineAPI: true });
      if (result.exceptionDetails) throw new Error('NATIVE_ELECTRON_EVALUATION_FAILED');
      return result.result.value;
    }
    if (observe) {
      await send('Runtime.enable');
      await send('Debugger.enable');
      entryBreakpoint = (await send('Debugger.setBreakpointByUrl', { lineNumber: 0, urlRegex: '/src/main\\.mjs$' })).breakpointId;
      await send('Runtime.runIfWaitingForDebugger');
      await budget.run(entry);
      await evaluate((electron, trace) => {
        const { app } = electron;
        globalThis.windowAudit = { ready: null, decisions: [], dialogs: [], failures: [] };
        const lifecycle = [];
        const record = name => {
          if (lifecycle.length < 100) lifecycle.push(name);
          if (trace) process.getBuiltinModule('fs').writeFileSync(trace, JSON.stringify(lifecycle), { mode: 0o600 });
        };
        for (const name of ['before-quit', 'will-quit', 'quit', 'window-all-closed']) app.on(name, () => record(name));
        process.on('uncaughtExceptionMonitor', () => record('uncaught-exception'));
        process.on('exit', () => record('node-exit'));
        app.once('ready', () => {
        record('ready');
        globalThis.windowAudit.ready = { userData: app.getPath('userData'), sessionData: app.getPath('sessionData') };
        const { session, dialog } = electron;
        const partition = session.fromPartition.bind(session);
        session.fromPartition = (...args) => {
          const current = partition(...args);
          if (!current.windowAuditInstalled) {
            current.windowAuditInstalled = true;
            const flush = current.cookies.flushStore.bind(current.cookies);
            current.cookies.flushStore = () => {
              record('cookie-flush-start');
              return flush().then(value => { record('cookie-flush-done'); return value; }, error => { record('cookie-flush-error'); throw error; });
            };
            const original = current.webRequest.onBeforeSendHeaders.bind(current.webRequest);
            current.webRequest.onBeforeSendHeaders = (filter, policy) => original(filter, (details, callback) => policy(details, result => {
              try {
                const audit = globalThis.windowAudit;
                let top = null; try { top = details.frame?.parent === null; } catch {}
                if (audit.decisions.length < 1000) audit.decisions.push({ type: details.resourceType,
                  path: new URL(details.url).pathname, frame: Boolean(details.frame), top,
                  initiator: details.initiatorOrigin ?? null, cancel: Boolean(result.cancel),
                  capability: Object.keys(result.requestHeaders ?? {}).some(key => key.toLowerCase() === 'x-loopcast-desktop-session') });
              } finally { callback(result); } // Exactly the production policy's decision.
            }));
          }
          return current;
        };
        const nativeDialog = dialog.showMessageBox.bind(dialog);
        dialog.showMessageBox = options => {
          record('native-error-dialog');
          globalThis.windowAudit.dialogs.push({ type: options.type, message: options.message, buttons: options.buttons });
          return nativeDialog(options); // Show the actual bounded native dialog.
        };
        });
        app.on('web-contents-created', (_event, contents) => {
          contents.on('render-process-gone', (_event, details) => record('renderer-gone-'+details.reason));
          contents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
            globalThis.windowAudit.failures.push({ code, isMainFrame });
          });
        });
      }, trace, false);
      await send('Debugger.removeBreakpoint', { breakpointId: entryBreakpoint });
      entryBreakpoint = undefined;
      await send('Debugger.resume');
    }
    await until(() => browserURL);
    browser = await chromium.connectOverCDP(browserURL, { timeout: 15000 });
    const context = browser.contexts()[0];
    async function firstPage() {
      try {
        const page = await until(() => context.pages().find(page => page.url().startsWith('http://127.0.0.1:')));
        await page.waitForLoadState('domcontentloaded', { timeout: 15000 }); return page;
      } catch {
        const diagnostic = observe ? await evaluate(() => ({ failures: globalThis.windowAudit.failures,
          denied: globalThis.windowAudit.decisions.filter(value => value.cancel).map(({ type, frame, top, initiator }) => ({ type, frame, top, hasInitiator: initiator !== null })) })).catch(() => ({})) : {};
        throw new Error('REAL_WINDOW_LOAD_FAILED ' + JSON.stringify(diagnostic));
      }
    }
    return { child, browser, context, evaluate, firstPage, exited, get status() { return status; },
      async quit() {
        const closing = new Budget(20000);
        while (!status && !debuggerWaiting) {
          await evaluate(({ app }) => { app.quit(); }).catch(() => {});
          if (!status) await closing.run(Promise.race([exited, new Promise(resolve => setTimeout(resolve, 100))]));
        }
        socket.close(); await browser.close();
        return closing.run(exited);
      },
      async detach() { socket.close(); await browser.close(); },
    };
  } catch (error) {
    socket?.close(); await browser?.close().catch(() => {});
    if (!status) child.kill('SIGKILL'); await exited;
    throw error;
  }
}
