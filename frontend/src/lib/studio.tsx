'use client';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { apiRequest } from './api';
import { StudioClient, type Capabilities } from './studio-client';
import { durablePendingStorage } from './pending-storage';

type ContextValue = { client: StudioClient; caps: Capabilities | null; error: unknown; refreshSignal: number; refresh: () => void; busy: Set<string>; perform: <T>(id: string, fn: () => Promise<T>) => Promise<T> };
const Context = createContext<ContextValue | null>(null);
export function useStudio() { const value = useContext(Context); if (!value) throw Error('Studio provider missing'); return value; }

export function StudioProvider({ children }: { children: ReactNode }) {
  const [client] = useState(() => new StudioClient());
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  useEffect(() => { try { client.hydrate(durablePendingStorage(window.localStorage, window.sessionStorage)); } catch { client.storageAvailable = false; } }, [client]);
  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<Capabilities>('/v1/studio/capabilities', { signal: controller.signal }).then((value) => { setCaps(value); setError(null); }).catch((e: unknown) => { if (!controller.signal.aborted) setError(e); });
    return () => controller.abort();
  }, [refreshSignal]);
  const perform = useCallback(async <T,>(id: string, fn: () => Promise<T>) => {
    setBusy((current) => new Set(current).add(id));
    try { return await fn(); }
    finally { setBusy((current) => { const next = new Set(current); next.delete(id); return next; }); setRefreshSignal((n) => n + 1); }
  }, []);
  const refresh = useCallback(() => setRefreshSignal((n) => n + 1), []);
  const value = useMemo(() => ({ client, caps, error, busy, perform, refreshSignal, refresh }), [client, caps, error, busy, perform, refreshSignal, refresh]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useStudioRead<T>(path: string, interval = 0) {
  const { refreshSignal } = useStudio();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try { const value = await apiRequest<T>(path, { signal: controller.signal }); if (!controller.signal.aborted) { setData(value); setError(null); } }
      catch (value) { if (!controller.signal.aborted) setError(value); }
      if (!controller.signal.aborted && interval) timer = setTimeout(read, interval);
    };
    void read(); return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval, refreshSignal, epoch]);
  return { data, error, reload: () => setEpoch((n) => n + 1) };
}
