'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { I18nextProvider, useTranslation } from 'react-i18next';
import { apiRequest, type RunSnapshot } from './api';
import { changeLanguage, engine, initializeLanguage } from './i18n';
import { currentPreferences, type Preferences } from './preferences';

export type PendingOperation = { key: string; signature: string; version: number; busy: boolean };
type WorkspaceContextValue = {
  ready: boolean;
  preferences: Preferences;
  preferencesAvailable: boolean;
  language: string;
  packError: boolean;
  generatorAvailable: boolean;
  online: boolean;
  syncKey: string;
  setSyncKey: (key: string) => void;
  refreshSignal: number;
  refresh: () => void;
  snapshots: Map<string, RunSnapshot>;
  accept: (snapshot: RunSnapshot) => boolean;
  listing: string[];
  setListing: (ids: string[]) => void;
  pageCursor: number | null;
  setPageCursor: (cursor: number | null) => void;
  pending: Map<string, PendingOperation>;
  putPending: (key: string, operation: PendingOperation | null) => void;
  drafts: Map<string, string | boolean>;
  setDraft: (key: string, value: string | boolean) => void;
  noticeCode: string | null;
  setNoticeCode: (code: string | null) => void;
  createOpen: boolean;
  openCreate: () => void;
  closeCreate: () => void;
  resetCreate: () => void;
};

const Context = createContext<WorkspaceContextValue | null>(null);

export function useWorkspace() {
  const context = useContext(Context);
  if (!context) throw new Error('LoopCast workspace context is missing');
  return context;
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [preferences, setPreferences] = useState<Preferences>({ version: 1, language: 'auto', theme: 'system' });
  const [preferencesAvailable, setPreferencesAvailable] = useState(true);
  const [language, setLanguage] = useState('en');
  const [packError, setPackError] = useState(false);
  const [generatorAvailable, setGeneratorAvailable] = useState(false);
  const [online, setOnline] = useState(true);
  const [syncKey, setSyncKey] = useState('syncing');
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [snapshots, setSnapshots] = useState<Map<string, RunSnapshot>>(new Map());
  const snapshotsRef = useRef(snapshots);
  snapshotsRef.current = snapshots;
  const [listing, setListing] = useState<string[]>([]);
  const [pageCursor, setPageCursor] = useState<number | null>(null);
  const [pending, setPending] = useState<Map<string, PendingOperation>>(new Map());
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const [drafts, setDrafts] = useState<Map<string, string | boolean>>(new Map());
  const [noticeCode, setNoticeCode] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const preferencesRef = useRef(preferences);

  const applyLanguage = useCallback(async (preferred: string) => {
    const result = await changeLanguage(preferred);
    setLanguage(result.language);
    setPackError(result.packError);
  }, []);

  useEffect(() => {
    let active = true;
    void (async () => {
      const result = await initializeLanguage();
      if (!active) return;
      const next = currentPreferences();
      preferencesRef.current = next;
      setPreferences(next);
      setPreferencesAvailable(window.workspacePreferences?.available ?? true);
      const languageResult = next.language === 'auto' ? result : await changeLanguage(next.language);
      if (!active) return;
      setLanguage(languageResult.language);
      setPackError(languageResult.packError);
      setReady(true);
      void apiRequest<{ video_generation: boolean }>('/v1/capabilities')
        .then((caps) => active && setGeneratorAvailable(caps.video_generation))
        .catch(() => undefined);
    })();
    const onPreferences = () => {
      const next = currentPreferences();
      const old = preferencesRef.current;
      preferencesRef.current = next;
      setPreferences(next);
      setPreferencesAvailable(window.workspacePreferences?.available ?? false);
      if (next.language !== old.language) void applyLanguage(next.language === 'auto' ? 'auto' : next.language);
    };
    const onLanguage = () => {
      if (preferencesRef.current.language === 'auto') void applyLanguage('auto');
    };
    const onOffline = () => { setOnline(false); setSyncKey('offline'); };
    const onOnline = () => { setOnline(true); setSyncKey('syncing'); setRefreshSignal((value) => value + 1); };
    window.addEventListener('preferenceschange', onPreferences);
    window.addEventListener('languagechange', onLanguage);
    window.addEventListener('offline', onOffline);
    window.addEventListener('online', onOnline);
    return () => {
      active = false;
      window.removeEventListener('preferenceschange', onPreferences);
      window.removeEventListener('languagechange', onLanguage);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('online', onOnline);
    };
  }, [applyLanguage]);

  const accept = useCallback((snapshot: RunSnapshot) => {
    const current = snapshotsRef.current.get(snapshot.run_id);
    if (!/^[a-f0-9]{32}$/.test(snapshot.run_id)
      || !Number.isSafeInteger(snapshot.state_version)
      || !Number.isSafeInteger(snapshot.last_event_id)
      || (current && (snapshot.state_version < current.state_version || snapshot.last_event_id < current.last_event_id))) return false;
    const next = new Map(snapshotsRef.current);
    next.set(snapshot.run_id, snapshot);
    snapshotsRef.current = next;
    setSnapshots(next);
    return true;
  }, []);

  const putPending = useCallback((key: string, operation: PendingOperation | null) => {
    const next = new Map(pendingRef.current);
    if (operation) next.set(key, operation); else next.delete(key);
    pendingRef.current = next;
    setPending(next);
  }, []);
  const setDraft = useCallback((key: string, value: string | boolean) => {
    setDrafts((current) => new Map(current).set(key, value));
  }, []);

  const value = useMemo<WorkspaceContextValue>(() => ({
    ready, preferences, preferencesAvailable, language, packError, generatorAvailable, online, syncKey,
    setSyncKey, refreshSignal, refresh: () => setRefreshSignal((current) => current + 1),
    snapshots, accept, listing, setListing, pageCursor, setPageCursor, pending, putPending,
    drafts, setDraft, noticeCode, setNoticeCode, createOpen, openCreate: () => setCreateOpen(true),
    closeCreate: () => setCreateOpen(false),
    resetCreate: () => { setCreateOpen(false); setNoticeCode(null); },
  }), [ready, preferences, preferencesAvailable, language, packError, generatorAvailable, online, syncKey,
    refreshSignal, snapshots, accept, listing, pageCursor, pending, putPending, drafts, setDraft, noticeCode, createOpen]);

  if (!ready) {
    return <div className="startup" role="status">Loading workspace…</div>;
  }
  return <I18nextProvider i18n={engine}><Context.Provider value={value}>{children}</Context.Provider></I18nextProvider>;
}

export function useLocaleText() {
  return useTranslation();
}
