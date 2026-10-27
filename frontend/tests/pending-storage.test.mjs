import test from 'node:test';
import assert from 'node:assert/strict';
import { durablePendingStorage } from '../src/lib/pending-storage.ts';
class Storage {
  values = new Map();
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
test('concurrent windows preserve independent pending records and recover after a new window', () => {
  const disk = new Storage(), name = 'loopcast.studio.pending.v1';
  const a = durablePendingStorage(disk), b = durablePendingStorage(disk);
  a.getItem(name); b.getItem(name);
  a.setItem(name, JSON.stringify([{ key: 'first', body: { title: 'unknown first' } }]));
  b.setItem(name, JSON.stringify([{ key: 'second', body: { title: 'unknown second' } }]));
  a.setItem(name, '[]');
  const recovered = JSON.parse(durablePendingStorage(disk).getItem(name));
  assert.deepEqual(recovered, [{ key: 'second', body: { title: 'unknown second' } }]);
});
test('legacy session pending migrates once without overwriting a newer durable record', () => {
  const disk = new Storage(), session = new Storage(), name = 'loopcast.studio.pending.v1';
  session.setItem(name, JSON.stringify([{ key: 'first' }]));
  assert.deepEqual(JSON.parse(durablePendingStorage(disk, session).getItem(name)), [{ key: 'first' }]);
  assert.equal(session.getItem(name), null);
  assert.deepEqual(JSON.parse(durablePendingStorage(disk).getItem(name)), [{ key: 'first' }]);
});
