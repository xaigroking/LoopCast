// SPDX-License-Identifier: Apache-2.0
import { HostError } from './errors.mjs';
const codes = new Set(['OK', 'DIRECTORY_MISSING', 'DIRECTORY_UNSAFE', 'FILE_UNSAFE', 'PERMISSION_DENIED',
  'OWNER_HELD', 'MAINTENANCE_UNCONFIRMED', 'DESKTOP_CLEANUP_UNCONFIRMED', 'GUARD_UNSAFE', 'DIAGNOSTICS_UNAVAILABLE']);
export function parsePreflight(bytes, exitCode) {
  try {
    if (!Buffer.isBuffer(bytes) || bytes.length > 512 || bytes.some(byte => byte > 126 || byte < 32 && byte !== 10)) throw new Error();
    const raw = bytes.toString('ascii');
    if (!raw.endsWith('\n') || raw.indexOf('\n') !== raw.length - 1) throw new Error();
    const value = JSON.parse(raw);
    if (!value || Object.keys(value).sort().join(',') !== 'code,format' || value.format !== 'loopcast.desktop-preflight.v1' ||
        !codes.has(value.code) || exitCode !== (value.code === 'OK' ? 0 : 1)) throw new Error();
    if (raw !== JSON.stringify({ format: value.format, code: value.code }) + '\n') throw new Error();
    return value.code;
  } catch { throw new HostError('DIAGNOSTICS_UNAVAILABLE'); }
}
