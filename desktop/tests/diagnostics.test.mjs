// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { HostError, safeError, failureDetails, failureText } from '../src/errors.mjs';
import { parsePreflight } from '../src/preflight.mjs';
import { DesktopHost } from '../src/host.mjs';
import { config } from './fixture-port.mjs';
const frame = code => Buffer.from(JSON.stringify({ format: 'loopcast.desktop-preflight.v1', code })+'\n');
test('preflight frames require a single bounded canonical safe result and matching exit', () => {
  assert.equal(parsePreflight(frame('OK'), 0), 'OK');
  for (const code of ['DIRECTORY_MISSING','DIRECTORY_UNSAFE','FILE_UNSAFE','PERMISSION_DENIED','OWNER_HELD','DESKTOP_CLEANUP_UNCONFIRMED','GUARD_UNSAFE','DIAGNOSTICS_UNAVAILABLE']) assert.equal(parsePreflight(frame(code), 1), code);
  const malformed = [Buffer.alloc(513,65),frame('secret-token-canary'),Buffer.from('{"format":"loopcast.desktop-preflight.v1","code":"OWNER_HELD","code":"OK"}\n'),
    Buffer.from('{"format":"loopcast.desktop-preflight.v1","code":"OK","token":"secret-token-canary"}\n'),Buffer.concat([frame('OK'),frame('OK')]),Buffer.from([255]),Buffer.from(frame('OK').toString().replace('\n','\r\n'))];
  for (const bytes of malformed) assert.throws(() => parsePreflight(bytes,0), error => error.code === 'DIAGNOSTICS_UNAVAILABLE' && !error.message.includes('secret-token-canary'));
  assert.throws(() => parsePreflight(frame('OK'),1),/DIAGNOSTICS_UNAVAILABLE/);
  assert.throws(() => parsePreflight(frame('OWNER_HELD'),0),/DIAGNOSTICS_UNAVAILABLE/);
});
test('failure guidance keeps the primary cause and never serializes exception/config values', () => {
  const unsafe = new Error('/private/path secret-token-canary');unsafe.code='OWNER_HELD';
  assert.equal(safeError(unsafe).code,'START_FAILED');
  assert.equal(safeError(new HostError('toString')).code,'START_FAILED');
  for (const locale of ['en','zh-Hans','ar']) {
    const primary=new HostError('RESOURCE_DIGEST_MISMATCH');primary.stack='secret-stack-canary';
    const details=failureDetails(primary,locale,new HostError('STOP_UNCONFIRMED'));
    assert.ok(details.includes('RESOURCE_DIGEST_MISMATCH'));assert.ok(details.includes('STOP_UNCONFIRMED'));assert.ok(details.includes('desktop-diagnostics'));
    for (const secret of ['secret-token-canary','secret-stack-canary','/private/path']) assert.equal((details+failureText(unsafe,locale)).includes(secret),false);
  }
});
test('startup retains its original safe error when cleanup diagnosis also fails', async () => {
  const host=new DesktopHost(config(),{environment:async()=>{throw new HostError('DIRECTORY_UNSAFE');},finish:async()=>({confirmed:false})});
  await assert.rejects(host.start(),error=>error.code==='DIRECTORY_UNSAFE');
  assert.equal(host.error.code,'STOP_UNCONFIRMED');assert.equal(host.state,'STOPPING');
  await assert.rejects(host.start(),/STOP_UNCONFIRMED/);
});
