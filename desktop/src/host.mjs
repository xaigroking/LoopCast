// SPDX-License-Identifier: Apache-2.0
import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import { Budget, HostError, safeError } from './errors.mjs';
import { createGateway } from './gateway.mjs';
import { capture, observeExit, readiness } from './protocol.mjs';
import { equalProof, healthProof } from './authentication.mjs';

const wrapper = fileURLToPath(new URL('./next-child.cjs', import.meta.url));
const environmentKeys = new Set(['PATH', 'HOME', 'USERPROFILE', 'TMP', 'TEMP', 'TMPDIR', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'LANG', 'LC_ALL']);
function minimalEnvironment(value) {
  if (!value || Object.entries(value).some(([key, item]) => !environmentKeys.has(key) || typeof item !== 'string' || item.includes('\0'))) {
    throw new HostError('PLATFORM_UNAVAILABLE');
  }
  return { ...value };
}

function request(origin, path, token, budget, { head = false, publicOrigin = origin, probe } = {}) {
  const timeout = budget.remaining();
  return new Promise((resolve, reject) => {
    let length = 0; const chunks = [];
    const req = http.request(`${origin}${path}`, {
      method: head ? 'HEAD' : 'GET', agent: false,
      headers: { Host: publicOrigin.slice(7), ...(probe ?? { 'X-LoopCast-Desktop-Session': token }), 'X-Forwarded-Host': publicOrigin.slice(7), 'X-Forwarded-Proto': 'http' },
    }, response => {
      response.on('data', chunk => {
        length += chunk.length;
        if (length > 32768) req.destroy(new HostError('PROTOCOL_INVALID'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks).toString('utf8') }));
    });
    const abort = () => req.destroy(new HostError('START_FAILED'));
    budget.signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => req.destroy(new HostError('START_TIMEOUT')), timeout);
    req.once('close', () => { clearTimeout(timer); budget.signal?.removeEventListener('abort', abort); });
    req.on('error', reject); req.end();
  });
}

export class DesktopHost extends EventEmitter {
  constructor(config, treePort, { gatewayFactory = createGateway, startupDeadline } = {}) {
    super(); this.config = config; this.tree = treePort; this.gatewayFactory = gatewayFactory;
    this.state = 'STOPPED'; this.error = null; this.handles = []; this.startupDeadline = startupDeadline;
    this.gatewayToken = randomBytes(32).toString('hex'); this.apiToken = randomBytes(32).toString('hex');
  }
  transition(state) { this.state = state; this.emit('state', state); }
  start() {
    if (this.startPending) return this.startPromise;
    if (this.state === 'READY') return Promise.resolve(this.gateway.origin);
    if (this.startPromise && this.state === 'STARTING') return this.startPromise;
    if (this.state !== 'STOPPED') return Promise.reject(new HostError('STOP_UNCONFIRMED'));
    this.error = null; this.primaryError = null; this.stopPromise = null; this.handles = []; this.api = null; this.gateway = null;
    this.gatewayToken = randomBytes(32).toString('hex'); this.apiToken = randomBytes(32).toString('hex');
    this.controller = new AbortController(); this.transition('STARTING');
    this.startPending = true;
    this.startPromise = this.startInternal().catch(async error => {
      this.primaryError = error?.code === 'EADDRINUSE' ? new HostError('ENTRY_PORT_BUSY') : safeError(error);
      this.error ??= this.primaryError;
      this.controller.abort();
      await this.cleanup();
      throw this.primaryError;
    }).finally(() => { this.startPending = false; });
    return this.startPromise;
  }
  launch(executable, argv, env, budget, { service = false, privateReady = false, api = false } = {}) {
    budget.remaining();
    const handle = this.tree.launch({ executable, argv, cwd: this.config.sessionDir, env,
      stdio: ['pipe', privateReady ? 'ignore' : 'pipe', 'pipe', ...(privateReady ? ['pipe'] : [])],
      deadline: budget.end, signal: budget.signal });
    if (!handle?.child || typeof handle.closeTree !== 'function') throw new HostError('PLATFORM_UNAVAILABLE');
    const record = { handle, service, api, exit: observeExit(handle.child) };
    this.handles.push(record); // Own the handle before awaiting readiness.
    handle.child.stdin.on('error', () => {});
    handle.child.stderr?.resume(); // Raw component output never reaches a log or UI.
    if (service) {
      record.protocol = readiness(privateReady ? handle.child.stdio[3] : handle.child.stdout);
      record.protocol.events.on('fault', error => this.componentFault(error));
      record.protocol.stopped.then(() => this.componentFault(new HostError('COMPONENT_EXITED')));
      record.exit.exited.then(() => this.componentFault(new HostError('COMPONENT_EXITED')));
    }
    return record;
  }
  componentFault(error) {
    if (this.state === 'STOPPING' || this.state === 'STOPPED') return;
    this.error = error; this.controller.abort();
    if (this.state === 'READY') {
      this.emit('fault', error);
      void this.stop();
    }
  }
  async probe(executable, argv, env, budget) {
    const record = this.launch(executable, argv, env, budget);
    const output = await capture(record.handle, budget);
    const evidence = await budget.run(record.handle.closeTree(budget.end));
    record.closed = evidence?.confirmed === true;
    if (!record.closed) throw new HostError('STOP_UNCONFIRMED');
    return output;
  }
  async waitReady(record, budget) {
    return budget.run(Promise.race([record.protocol.ready, record.exit.exited.then(() => { throw new HostError('COMPONENT_EXITED'); })]));
  }
  async startInternal() {
    const config = this.config;
    const budget = new Budget(config.startTimeoutMs, this.controller.signal);
    if (this.startupDeadline !== undefined) budget.end = Math.min(budget.end, this.startupDeadline);
    const env = await budget.run(this.tree.environment({ dataDir: config.dataDir, sessionDir: config.sessionDir, deadline: budget.end, signal: budget.signal }));
    const apiEnv = minimalEnvironment(env.api), webEnv = minimalEnvironment(env.web);
    const nodeVersion = await this.probe(config.node.path, ['--version'], webEnv, budget);
    if (nodeVersion.trim() !== `v${config.nodeVersion}`) throw new HostError('VERSION_MISMATCH');
    let info;
    try { info = JSON.parse(await this.probe(config.python.path, ['-I', '-m', 'loopcast', 'platform-info'], apiEnv, budget)); }
    catch (error) { if (error instanceof HostError) throw error; throw new HostError('PROTOCOL_INVALID'); }
    if (info.format !== 'loopcast.platform.v1' || info.source_revision !== config.apiRevision) throw new HostError('VERSION_MISMATCH');
    for (const capability of ['file_access', 'owner_lock', 'sqlite_access', 'process_tree', 'managed_stop', 'desktop_session']) {
      if (info.capabilities?.[capability] !== 'implemented') throw new HostError('CAPABILITY_UNAVAILABLE');
    }
    await budget.run(this.gatewayFactory({ gatewayToken: this.gatewayToken, apiToken: this.apiToken, port: config.gatewayPort }).then(async gateway => {
      if (this.controller.signal.aborted || this.state !== 'STARTING') { await gateway.close(); throw new HostError('START_FAILED'); }
      this.gateway = gateway;
    }));
    const argv = ['-I', '-m', 'loopcast', 'serve', '--host', '127.0.0.1', '--port', '0',
      '--data-dir', config.dataDir, '--managed-stdin', '--desktop-session-stdin',
      '--public-origin', this.gateway.origin, '--trusted-proxy', '127.0.0.1/32'];
    for (const name of ['generator', 'media', 'social', 'schedule', 'inspection']) {
      if (config.configs?.[name]) argv.push(`--${name}-config`, config.configs[name]);
    }
    this.api = this.launch(config.python.path, argv, apiEnv, budget, { service: true, api: true });
    this.api.handle.child.stdin.write(`${JSON.stringify({ format: 'loopcast.desktop-session.v1', token: this.apiToken })}\n`);
    const apiOrigin = await this.waitReady(this.api, budget);
    // API Host and forwarded identity must name the gateway, never its own port.
    const nonce = randomBytes(32).toString('hex');
    const health = await request(apiOrigin, '/healthz', undefined, budget, { publicOrigin: this.gateway.origin, probe: {
      'X-LoopCast-Desktop-Probe': nonce,
      'X-LoopCast-Desktop-Proof': healthProof(this.apiToken, 'request', nonce, apiOrigin, this.gateway.origin, config.apiRevision),
    } });
    let healthy = false;
    try {
      healthy = health.status === 200 && JSON.parse(health.body).ready === true &&
        equalProof(health.headers['x-loopcast-desktop-proof'], healthProof(this.apiToken, 'response', nonce, apiOrigin, this.gateway.origin, config.apiRevision));
    } catch {}
    if (!healthy) throw new HostError('START_FAILED');
    const web = this.launch(config.node.path, [wrapper, config.nextRoot, config.webRevision],
      { ...webEnv, NODE_ENV: 'production', NEXT_TELEMETRY_DISABLED: '1' }, budget, { service: true, privateReady: true });
    const webOrigin = await this.waitReady(web, budget);
    this.gateway.setTargets({ api: apiOrigin, web: webOrigin });
    const page = await request(this.gateway.origin, '/', this.gatewayToken, budget, { head: true });
    if (page.status !== 200 || !page.headers['content-security-policy']?.includes("script-src 'self' 'nonce-")) throw new HostError('START_FAILED');
    budget.remaining();
    if (this.error) throw this.error;
    this.transition('READY'); return this.gateway.origin;
  }
  stop() {
    if (this.stopPromise) return this.stopPromise;
    if (this.state === 'STOPPED') return Promise.resolve({ confirmed: true });
    this.controller.abort();
    // Cleanup owns all resources synchronously registered by launch. Startup's
    // catch also joins this same cleanup; it cannot create a second shutdown.
    return this.cleanup();
  }
  cleanup() {
    if (this.stopPromise) return this.stopPromise;
    this.transition('STOPPING'); this.gateway?.suspend();
    this.stopPromise = this.cleanupInternal();
    return this.stopPromise;
  }
  async cleanupInternal() {
    const budget = new Budget(this.config.stopTimeoutMs);
    let confirmed = true;
    const api = this.api;
    if (api) {
      try {
        if (!api.exit.result) api.handle.child.stdin.end('STOP\n');
        await new Budget(this.config.stopTimeoutMs * 0.7).run(Promise.all([api.protocol.stopped, api.exit.exited]));
        if (!api.protocol.confirmed || api.exit.result?.code !== 0) confirmed = false;
      } catch { confirmed = false; }
    }
    // closeTree must still receive the original deadline when STOP has consumed
    // the budget. It must terminate/report synchronously owned trees, never
    // reset the timeout or claim success for only the leader.
    const outcomes = await Promise.all(this.handles.filter(record => !record.closed).map(async record => {
      if (record !== api && record.service && !record.exit.result) record.handle.child.stdin.end('STOP\n');
      try {
        const result = await new Budget(Math.max(1, budget.end - performance.now())).run(record.handle.closeTree(budget.end));
        return result?.confirmed === true;
      } catch { return false; }
    }));
    if (outcomes.some(value => !value)) confirmed = false;
    if (this.tree.finish) {
      try {
        const result = await new Budget(Math.max(1, budget.end - performance.now())).run(this.tree.finish({ confirmed, deadline: budget.end }));
        if (!result?.confirmed) confirmed = false;
      } catch { confirmed = false; }
    }
    try { await this.gateway?.close(); } catch { confirmed = false; }
    if (confirmed) this.transition('STOPPED');
    else { this.error = new HostError('STOP_UNCONFIRMED'); this.emit('fault', this.error); }
    return { confirmed };
  }
}
