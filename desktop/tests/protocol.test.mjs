// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { readiness } from '../src/protocol.mjs';

test('split READY frames and cancellation STOPPED before readiness are handled independently', async () => {
  const stream = new PassThrough(), monitor = readiness(stream);
  stream.write('READY http://127.'); stream.write('0.0.1:12345\n');
  assert.equal(await monitor.ready, 'http://127.0.0.1:12345');
  stream.end('STOPPED\n'); await monitor.stopped;
  assert.equal(monitor.confirmed, true);
  const cancelled = new PassThrough(), state = readiness(cancelled);
  cancelled.end('STOPPED\n');
  await state.stopped; await assert.rejects(state.ready, /COMPONENT_EXITED/);
  assert.equal(state.confirmed, true);
});

test('EOF on a running component reports failure even if its process has not exited', async () => {
  const stream = new PassThrough(), monitor = readiness(stream);
  stream.write('READY http://127.0.0.1:12345\n'); await monitor.ready;
  const fault = new Promise(resolve => monitor.events.once('fault', resolve));
  stream.end();
  assert.equal((await fault).code, 'COMPONENT_EXITED'); assert.equal(monitor.confirmed, false);
});

test('high-bit bytes, CRLF, zero ports and trailing output cannot forge valid readiness', async () => {
  for (const value of [Buffer.from([0xd2, 0x45, 0x41, 0x44, 0x59, 0x0a]), Buffer.from('READY http://127.0.0.1:1234\r\n'), Buffer.from('READY http://127.0.0.1:0\n')]) {
    const stream = new PassThrough(), monitor = readiness(stream); stream.end(value);
    await assert.rejects(monitor.ready, /PROTOCOL_INVALID/);
  }
  const stream = new PassThrough(), monitor = readiness(stream);
  const fault = new Promise(resolve => monitor.events.once('fault', resolve));
  stream.end('READY http://127.0.0.1:1234\nSTOPPED\nextra\n');
  await fault; assert.equal(monitor.confirmed, false);
});
