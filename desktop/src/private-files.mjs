// SPDX-License-Identifier: Apache-2.0
import { constants, openSync, closeSync, fstatSync, lstatSync, readFileSync, writeFileSync, fsyncSync, mkdirSync, readdirSync } from 'node:fs';
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { isAbsolute, join, parse, relative, sep } from 'node:path';
import { HostError } from './errors.mjs';

export const hash = bytes => createHash('sha256').update(bytes).digest('hex');
export const inode = info => [String(info.dev), String(info.ino)];
export const sameInode = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const contains = (a, b) => { const p = relative(a, b); return p === '' || p !== '..' && !p.startsWith(`..${sep}`) && !isAbsolute(p); };
function privateInfo(info, directory) {
  if (!(directory ? info.isDirectory() : info.isFile() && info.nlink === 1) || info.uid !== process.getuid() || (info.mode & 0o077)) throw new HostError('FILE_UNSAFE');
}

export function holdDirectory(path) {
  if (!isAbsolute(path) || path.split(sep).includes('..') || path === '/') throw new HostError('PATH_UNSAFE');
  let fd = openSync('/', constants.O_RDONLY | constants.O_DIRECTORY);
  try {
    for (const component of path.split(sep).filter(Boolean)) {
      const child = openSync(`/proc/self/fd/${fd}/${component}`, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      closeSync(fd); fd = child;
    }
    privateInfo(fstatSync(fd), true);
    const identity = inode(fstatSync(fd));
    return { path, fd, identity, anchor: `/proc/self/fd/${fd}`,
      check() { const next = holdDirectory(path); try { if (!sameInode(next.identity, identity)) throw new HostError('PATH_UNSAFE'); } finally { next.close(); } },
      close() { if (this.fd !== null) { closeSync(this.fd); this.fd = null; } } };
  } catch (error) { closeSync(fd); throw error; }
}

export function readPrivate(path, max = 65536) {
  const parent = holdDirectory(parse(path).dir); let fd;
  try {
    fd = openSync(`${parent.anchor}/${parse(path).base}`, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const before = fstatSync(fd); privateInfo(before, false);
    if (before.size > max) throw new HostError('FILE_UNSAFE');
    const bytes = readFileSync(fd), after = fstatSync(fd), named = lstatSync(`${parent.anchor}/${parse(path).base}`, { bigint: false });
    if (bytes.length > max || !sameInode(inode(before), inode(named)) || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) throw new HostError('FILE_UNSAFE');
    parent.check(); return bytes;
  } finally { if (fd !== undefined) closeSync(fd); parent.close(); }
}

export function writeNew(parent, name, bytes) {
  if (typeof name !== 'string' || !name || ['.','..'].includes(name) || name.includes('/') || name.includes('\\') || name.includes('\0')) throw new HostError('PATH_UNSAFE');
  parent.check();
  const fd = openSync(`${parent.anchor}/${name}`, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, bytes); fsyncSync(fd); } finally { closeSync(fd); }
  fsyncSync(parent.fd); parent.check();
}

export class ReceiptJournal {
  constructor(root) {
    const parent = holdDirectory(root);
    try { try { mkdirSync(`${parent.anchor}/maintenance`, { mode: 0o700 }); fsyncSync(parent.fd); } catch (e) { if (e.code !== 'EEXIST') throw e; } }
    finally { parent.close(); }
    this.root = holdDirectory(join(root, 'maintenance'));
    try {
      try { this.key = readPrivate(join(this.root.path, 'key'), 32); }
      catch (error) { if (error.code !== 'ENOENT') throw error; this.key = randomBytes(32); writeNew(this.root, 'key', this.key); }
      if (this.key.length !== 32) throw new HostError('FILE_UNSAFE');
      this.records();
    } catch (e) { this.close(); throw e; }
  }
  records() {
    this.root.check();
    const names = readdirSync(this.root.anchor);
    if (names.length > 10000 || names.some(name => name !== 'key' && !/^[a-f0-9]{32}-[A-Z_]+\.json$/.test(name))) throw new HostError('MAINTENANCE_UNCONFIRMED');
    return names.filter(name => name !== 'key').map(name => {
      const raw = readPrivate(join(this.root.path, name)), envelope = JSON.parse(raw);
      const bytes = Buffer.from(JSON.stringify(envelope.receipt));
      const mac = createHmac('sha256', this.key).update(bytes).digest();
      if (!raw.equals(Buffer.from(JSON.stringify(envelope))) || Object.keys(envelope).sort().join(',') !== 'hmac,receipt' || typeof envelope.hmac !== 'string' || !/^[a-f0-9]{64}$/.test(envelope.hmac) ||
          !timingSafeEqual(mac, Buffer.from(envelope.hmac, 'hex')) || name !== `${envelope.receipt.operation_id}-${envelope.receipt.state}.json`) throw new HostError('MAINTENANCE_UNCONFIRMED');
      return envelope.receipt;
    });
  }
  assertClear() {
    const records = this.records();
    if (records.some(value => value.state === 'UNKNOWN_TREE') || records.some(value => value.state === 'PREPARED' && !records.some(other => other.operation_id === value.operation_id && other.state !== 'PREPARED'))) throw new HostError('MAINTENANCE_UNCONFIRMED');
  }
  append(receipt) {
    const bytes = Buffer.from(JSON.stringify(receipt));
    writeNew(this.root, `${receipt.operation_id}-${receipt.state}.json`, JSON.stringify({ receipt, hmac: createHmac('sha256', this.key).update(bytes).digest('hex') }));
    return receipt;
  }
  close() { this.root?.close(); this.key?.fill(0); }
}
