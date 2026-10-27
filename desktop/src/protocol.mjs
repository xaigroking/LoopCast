// SPDX-License-Identifier: Apache-2.0
import { EventEmitter } from 'node:events';
import { HostError } from './errors.mjs';

export function readiness(stream) {
  const events = new EventEmitter();
  let buffer = Buffer.alloc(0), ready = false, stopped = false, invalid = false;
  let resolveReady, rejectReady, resolveStopped;
  const readyPromise = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  // Attach immediately: an early child event must not become an unhandled rejection.
  readyPromise.catch(() => {});
  const stoppedPromise = new Promise(resolve => { resolveStopped = resolve; });
  function fail(code = 'PROTOCOL_INVALID') {
    if (invalid) return;
    invalid = true; rejectReady(new HostError(code));
    events.emit('fault', new HostError(code));
  }
  stream.on('data', chunk => {
    if (invalid) return;
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.includes(10)) {
      const index = buffer.indexOf(10);
      const line = buffer.subarray(0, index).toString('ascii');
      const bytes = buffer.subarray(0, index); buffer = buffer.subarray(index + 1);
      if (bytes.length > 256 || bytes.some(b => b < 32 || b > 126)) { fail(); return; }
      const match = /^READY http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/.exec(line);
      if (match && !ready && !stopped && Number(match[1]) <= 65535) {
        ready = true; resolveReady(`http://127.0.0.1:${match[1]}`);
      } else if (line === 'STOPPED' && !stopped) {
        stopped = true; resolveStopped();
      } else { fail(); return; }
    }
    if (buffer.length > 256) fail();
  });
  stream.on('error', () => fail());
  stream.on('end', () => {
    if (buffer.length) fail();
    else if (!stopped) fail('COMPONENT_EXITED');
    // A cancelled startup can stop safely before it ever becomes READY.
    if (!ready) rejectReady(new HostError('COMPONENT_EXITED'));
  });
  return { ready: readyPromise, stopped: stoppedPromise, events, get confirmed() { return stopped && !invalid; } };
}

export function observeExit(child) {
  let result;
  const exited = new Promise(resolve => {
    child.once('exit', (code, signal) => { result = { code, signal }; resolve(result); });
    child.once('error', () => { result = { code: null, signal: null }; resolve(result); });
  });
  return { exited, get result() { return result; } };
}

export async function capture(handle, budget, limit = 32768) {
  const child = handle.child, exit = observeExit(child); let size = 0;
  const chunks = [];
  const output = new Promise((resolve, reject) => {
    child.stdout.on('data', chunk => {
      size += chunk.length;
      if (size > limit) reject(new HostError('PROTOCOL_INVALID'));
      else chunks.push(chunk);
    });
    child.stdout.once('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    child.stdout.once('error', () => reject(new HostError('PROTOCOL_INVALID')));
  });
  child.stderr?.resume();
  const [text, status] = await budget.run(Promise.all([output, exit.exited]));
  if (status.code !== 0) throw new HostError('COMPONENT_EXITED');
  return text;
}
