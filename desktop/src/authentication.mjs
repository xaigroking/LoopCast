// SPDX-License-Identifier: Apache-2.0
import { createHmac, timingSafeEqual } from 'node:crypto';

// Health probes use a scoped MAC, so a corrupted READY port never receives the
// API's bearer capability. Different request/response labels prevent reflection.
export function healthProof(token, direction, nonce, apiOrigin, publicOrigin, revision) {
  return createHmac('sha256', token).update(JSON.stringify([
    'loopcast.desktop-health.v1', direction, nonce, apiOrigin, publicOrigin, revision,
  ])).digest('hex');
}
export function equalProof(actual, expected) {
  if (typeof actual !== 'string' || !/^[a-f0-9]{64}$/.test(actual)) return false;
  return timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'));
}
