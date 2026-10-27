// Per-operation records prevent one window's snapshot from erasing another's.
const collection = 'loopcast.studio.pending.v1';
const prefix = 'loopcast.pending.operation.v1.';
export function durablePendingStorage(storage: Storage, legacy?: Storage) {
  let observed = new Set<string>();
  return {
    getItem(key: string) {
      if (key !== collection) return storage.getItem(key) ?? legacy?.getItem(key) ?? null;
      const old = JSON.parse(legacy?.getItem(collection) ?? '[]') as Array<{ key: string }>;
      for (const value of old) if (typeof value?.key === 'string' && !storage.getItem(prefix + value.key)) storage.setItem(prefix + value.key, JSON.stringify(value));
      legacy?.removeItem(collection);
      const values = [];
      for (let index = 0; index < storage.length; index++) {
        const name = storage.key(index);
        if (name?.startsWith(prefix)) values.push(JSON.parse(storage.getItem(name) ?? 'null'));
      }
      observed = new Set(values.map(value => value.key));
      return JSON.stringify(values);
    },
    setItem(key: string, raw: string) {
      if (key !== collection) { storage.setItem(key, raw); return; }
      const values = JSON.parse(raw) as Array<{ key: string }>;
      const next = new Set(values.map(value => value.key));
      for (const value of values) storage.setItem(prefix + value.key, JSON.stringify(value));
      for (const key of observed) if (!next.has(key)) storage.removeItem(prefix + key);
      observed = next;
    },
    removeItem(key: string) { storage.removeItem(key); legacy?.removeItem(key); },
  };
}
