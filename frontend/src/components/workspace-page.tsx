'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest, ApiError, requestKey, type RunAction, type RunCreate, type RunPage, type RunSnapshot } from '@/lib/api';
import { locales } from '@/lib/i18n';
import { currentPreferences, type Preferences } from '@/lib/preferences';
import { useWorkspace } from '@/lib/workspace';
import { subscribeEvents } from '@/lib/events';

const terminal = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
const settled = (run: RunSnapshot) => terminal.has(run.state) && !run.publications.some((item) => ['PREPARED', 'SENDING'].includes(item.state));
const runURL = (id: string) => `/runs/${encodeURIComponent(id)}`;

type Kind = 'list' | 'detail' | 'settings';

export function WorkspacePage({ kind, runId }: { kind: Kind; runId?: string }) {
  const router = useRouter();
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const pathname = kind === 'detail' ? runURL(runId ?? '') : kind === 'settings' ? '/settings' : '/';
  const [pageError, setPageError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const generation = useRef(0);
  const subscription = useRef<{ close: () => void } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const run = runId ? workspace.snapshots.get(runId) : undefined;
  const rows = workspace.listing.map((id) => workspace.snapshots.get(id)).filter((item): item is RunSnapshot => Boolean(item));

  useEffect(() => {
    if (kind === 'settings') {
      setLoading(false);
      return;
    }
    const currentGeneration = ++generation.current;
    let active = true;
    const requests = new AbortController();
    if (subscription.current) subscription.current.close();
    subscription.current = null;
    if (timer.current) clearTimeout(timer.current);
    setPageError(null);
    setLoading(true);
    workspace.setSyncKey(navigator.onLine ? 'syncing' : 'offline');

    const loadList = async (more = false) => {
      try {
        const query = new URLSearchParams({ limit: '30' });
        if (more && workspace.pageCursor) query.set('before', String(workspace.pageCursor));
        const page = await apiRequest<RunPage>(`/v1/runs?${query.toString()}`, { signal: requests.signal });
        if (!active || generation.current !== currentGeneration) return;
        page.items.forEach(workspace.accept);
        const ids = page.items.map((item) => item.run_id);
        workspace.setListing(more ? [...new Set([...workspace.listing, ...ids])] : [...new Set([...ids, ...workspace.listing])]);
        workspace.setPageCursor(page.next_cursor ?? null);
        setHasMore(page.next_cursor !== null);
        setPageError(null);
        setLoading(false);
        workspace.setSyncKey(navigator.onLine ? 'polling' : 'offline');
      } catch (error) {
        if (!active || generation.current !== currentGeneration) return;
        const apiError = error instanceof ApiError ? error : null;
        if (kind === 'detail' || workspace.listing.length === 0) setPageError(apiError?.code ?? 'INTERNAL_ERROR');
        setLoading(false);
        workspace.setSyncKey('offline');
      }
      if (active && generation.current === currentGeneration && navigator.onLine) {
        timer.current = setTimeout(() => void loadList(), 1500);
      }
    };

    const loadDetail = async () => {
      if (!runId || !/^[a-f0-9]{32}$/.test(runId)) {
        setPageError('NOT_FOUND');
        setLoading(false);
        return;
      }
      try {
        const snapshot = await apiRequest<RunSnapshot>(`/v1/runs/${runId}`, { signal: requests.signal });
        if (!active || generation.current !== currentGeneration) return;
        if (snapshot.run_id !== runId || !workspace.accept(snapshot)) {
          const latest = workspace.getSnapshot(runId);
          if (!latest || latest.run_id !== runId) throw new ApiError(500, 'INTERNAL_ERROR');
        }
        const latest = workspace.getSnapshot(runId) ?? snapshot;
        setPageError(null);
        setLoading(false);
        if (settled(latest)) {
          workspace.setSyncKey('settled');
          return;
        }
        if (!navigator.onLine) {
          workspace.setSyncKey('offline');
          return;
        }
        let cursor = latest.last_event_id;
        subscription.current = subscribeEvents({
          url: () => `/v1/runs/${runId}/events?after=${cursor}`,
          state: (value) => active && workspace.setSyncKey(value),
          receive: (event) => {
            const value = JSON.parse(event.data) as RunSnapshot;
            if (value.run_id !== runId || (event.lastEventId && Number(event.lastEventId) !== value.last_event_id)) throw new Error('INVALID_EVENT');
            const accepted = workspace.accept(value);
            if (accepted) cursor = Math.max(cursor, value.last_event_id);
            return accepted && settled(value);
          },
        });
      } catch (error) {
        if (!active || generation.current !== currentGeneration) return;
        const apiError = error instanceof ApiError ? error : null;
        if (apiError?.snapshot) workspace.accept(apiError.snapshot);
        setPageError(apiError?.code ?? 'INTERNAL_ERROR');
        setLoading(false);
        workspace.setSyncKey(navigator.onLine ? 'requestError' : 'offline');
      }
    };

    if (!navigator.onLine) setLoading(false);
    else if (kind === 'list') void loadList();
    else void loadDetail();
    return () => {
      active = false;
      requests.abort();
      if (subscription.current) subscription.current.close();
      subscription.current = null;
      if (timer.current) clearTimeout(timer.current);
    };
  // Route and explicit refresh are the only reasons to replace subscriptions.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, runId, workspace.refreshSignal, workspace.online]);

  async function loadMore() {
    if (loadingMore || !workspace.pageCursor) return;
    setLoadingMore(true);
    try {
      const page = await apiRequest<RunPage>(`/v1/runs?limit=30&before=${workspace.pageCursor}`);
      page.items.forEach(workspace.accept);
      workspace.setListing([...new Set([...workspace.listing, ...page.items.map((item) => item.run_id)])]);
      workspace.setPageCursor(page.next_cursor ?? null);
      setHasMore(page.next_cursor !== null);
    } catch (error) {
      workspace.setNoticeCode(error instanceof ApiError ? error.code : 'INTERNAL_ERROR');
    } finally {
      setLoadingMore(false);
    }
  }

  async function command(id: string, action: RunAction) {
    const current = workspace.snapshots.get(id);
    const identity = `${id}:${action}`;
    let operation = workspace.pending.get(identity);
    if (!current || operation?.busy || !current.allowed_actions[action].enabled) return;
    if (!operation && action === 'cancel' && !window.confirm(t('cancelConfirm'))) return;
    try {
      operation ??= { key: requestKey(), signature: String(current.state_version), version: current.state_version, busy: false };
    } catch {
      workspace.setNoticeCode('SECURE_RANDOM_UNAVAILABLE');
      return;
    }
    operation = { ...operation, busy: true };
    workspace.putPending(identity, operation);
    try {
      const result = await apiRequest<RunSnapshot>(`/v1/runs/${id}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.key },
        body: JSON.stringify({ expected_version: operation.version }),
      });
      if (result.run_id !== id) throw new ApiError(500, 'INTERNAL_ERROR');
      workspace.accept(result);
      workspace.putPending(identity, null);
      if (kind === 'list') workspace.refresh();
    } catch (error) {
      if (error instanceof ApiError) {
        workspace.putPending(identity, null);
        if (error.snapshot) workspace.accept(error.snapshot);
      } else {
        workspace.putPending(identity, { ...operation, busy: false });
      }
      workspace.setNoticeCode(error instanceof ApiError ? error.code : 'INTERNAL_ERROR');
      workspace.refresh();
    }
  }

  async function publicationCommand(current: RunSnapshot, intentId: string | null, action: 'dispatch' | 'verify' | null, extra: Record<string, string>) {
    const identity = `${intentId ?? current.run_id}:${action ?? 'target'}`;
    let operation = workspace.pending.get(identity);
    if (operation?.busy) return;
    const signature = JSON.stringify(extra);
    try {
      if (!operation || operation.signature !== signature) {
        operation = { key: requestKey(), signature, version: current.state_version, busy: false };
      }
    } catch {
      workspace.setNoticeCode('SECURE_RANDOM_UNAVAILABLE');
      return;
    }
    operation = { ...operation, busy: true };
    workspace.putPending(identity, operation);
    const path = `/v1/runs/${current.run_id}/publications${intentId ? `/${intentId}/${action}` : ''}`;
    try {
      const result = await apiRequest<RunSnapshot>(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.key },
        body: JSON.stringify({ ...extra, expected_version: operation.version }),
      });
      workspace.accept(result);
      workspace.putPending(identity, null);
    } catch (error) {
      if (error instanceof ApiError) {
        workspace.putPending(identity, null);
        if (error.snapshot) workspace.accept(error.snapshot);
      } else workspace.putPending(identity, { ...operation, busy: false });
      workspace.setNoticeCode(error instanceof ApiError ? error.code : 'INTERNAL_ERROR');
      workspace.refresh();
    }
  }

  if (kind === 'settings') return <SettingsView />;
  if (loading && (kind === 'detail' ? !run : rows.length === 0)) return <p>{t('loading')}</p>;
  if (pageError) return <ErrorView code={pageError} />;
  if (kind === 'list') return <RunList runs={rows} hasMore={hasMore} loading={loadingMore} command={command} loadMore={loadMore} />;
  if (!run) return <ErrorView code="NOT_FOUND" />;
  return <RunDetail run={run} command={command} publicationCommand={publicationCommand} />;
}

function SectionHeading({ title, subtitle, withCreate = false }: { title: string; subtitle: string; withCreate?: boolean }) {
  const { t } = useTranslation();
  const workspace = useWorkspace();
  return <section className="heading"><div><div className="eyebrow">{t('workspace')}</div><h1 dir="auto">{title}</h1><p>{subtitle}</p></div>{withCreate && <button className="primary" type="button" onClick={workspace.openCreate}>{t('new')}</button>}</section>;
}

function Panel({ title, children }: { title: string; children?: React.ReactNode }) {
  return <section className="panel"><h2>{title}</h2>{children}</section>;
}

function State({ value }: { value: string }) {
  const { t } = useTranslation();
  return <span className="state" data-state={value}>{t(`state:${value}`)}</span>;
}

function RunList({ runs, hasMore, loading, command, loadMore }: { runs: RunSnapshot[]; hasMore: boolean; loading: boolean; command: (id: string, action: RunAction) => Promise<void>; loadMore: () => void }) {
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const active = runs.filter((item) => !settled(item)).length;
  const completed = runs.filter((item) => item.state === 'SUCCEEDED').length;
  return <>
    <SectionHeading title={t('console')} subtitle={t('subtitle')} withCreate />
    <div className="stats">
      {[[t('loaded'), runs.length], [t('active'), active], [t('completed'), completed]].map(([label, count]) => <div className="stat" key={String(label)}><strong>{new Intl.NumberFormat(i18n.language).format(Number(count))}</strong><span>{label}</span></div>)}
    </div>
    <div className="section-head"><h2>{t('recent')}</h2><small>{t('taskCount', { count: runs.length })}</small></div>
    {!runs.length && <section className="empty"><div className="empty-icon" aria-hidden="true">▤</div><h2>{t('empty')}</h2><p>{t('fixtureNotice')}</p></section>}
    <div className="cards">
      {runs.map((item) => <RunCard key={item.run_id} run={item} command={command} />)}
    </div>
    {hasMore && <button id="load-more" type="button" disabled={loading} onClick={loadMore}>{t('older')}</button>}
  </>;
}

function RunCard({ run, command }: { run: RunSnapshot; command: (id: string, action: RunAction) => Promise<void> }) {
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const actions = (menu = false) => <div className={menu ? 'menu' : 'row-actions direct-action'}>
    {(['cancel', 'retry'] as const).map((action) => <button key={action} type="button" className={action === 'cancel' ? 'danger' : ''} data-action={action} data-focus-key={`${run.run_id}${action}${menu ? 'menu' : ''}`} title={run.allowed_actions[action].enabled ? t(action) : t('unavailable')} disabled={!run.allowed_actions[action].enabled || workspace.pending.get(`${run.run_id}:${action}`)?.busy} onClick={() => void command(run.run_id, action)}>{t(action)}</button>)}
  </div>;
  const meta = `${t(run.mode === 'fixture' ? 'fixture' : 'unverified')} · ${t('attemptVersion', { number: new Intl.NumberFormat(i18n.language).format(run.attempts.length), version: new Intl.NumberFormat(i18n.language).format(run.state_version) })}`;
  return <article className="run-card" data-run-id={run.run_id}>
    <div className="run-icon" aria-hidden="true">▤</div>
    <div className="run-main"><Link className="run-title" dir="auto" href={runURL(run.run_id)}>{run.input.title}</Link><div className="run-meta">{meta}</div></div>
    <State value={run.state} />
    <div className="row-actions">
      {actions()}
      <details className="action-menu" open={workspace.drafts.get(`${run.run_id}:menu`) === true} onToggle={(event) => workspace.setDraft(`${run.run_id}:menu`, event.currentTarget.open)}>
        <summary aria-label={`${t('actions')} · ${run.input.title}`}>•••</summary>{actions(true)}
      </details>
    </div>
  </article>;
}

function RunDetail({ run, command, publicationCommand }: { run: RunSnapshot; command: (id: string, action: RunAction) => Promise<void>; publicationCommand: (run: RunSnapshot, intentId: string | null, action: 'dispatch' | 'verify' | null, extra: Record<string, string>) => Promise<void> }) {
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const number = (value: number) => new Intl.NumberFormat(i18n.language).format(value);
  const date = (value: string) => new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
  const stageNames: Record<string, string> = { prepare: 'prepare', execute: 'execute', report: 'report' };
  const scenarioNames: Record<string, string> = { success: 'success', fail_once: 'failOnce', fail: 'fail', slow: 'slow' };
  const headingActions = <div className="row-actions"><State value={run.state} />{(['cancel', 'retry'] as const).map((action) => <button key={action} type="button" className={action === 'cancel' ? 'danger' : ''} disabled={!run.allowed_actions[action].enabled || workspace.pending.get(`${run.run_id}:${action}`)?.busy} onClick={() => void command(run.run_id, action)}>{t(action)}</button>)}</div>;
  return <>
    <Link className="back" href="/">{t('back')}</Link>
    <section className="heading detail-title"><div><div className="eyebrow">{t('workspace')}</div><h1 dir="auto">{run.input.title}</h1><p>{t('attemptVersion', { number: number(run.attempts.length), version: number(run.state_version) })}</p></div>{headingActions}</section>
    <div className="detail-grid">
      <div>
        <Panel title={t('stages')}>
          {run.tasks.map((task, index) => <div className="stage" key={task.id}><span className="stage-number">{number(index + 1)}</span><span>{t(`state:${stageNames[task.stage] ?? task.stage}`)}</span><State value={task.state} /></div>)}
          {run.state === 'CANCEL_REQUESTED' && <p>{t('cancelConfirm')}</p>}
        </Panel>
        <Panel title={t('history')}>
          {[...run.attempts].reverse().map((attempt) => <div className="attempt" key={attempt.id}>
            <strong>{t('attempt', { number: number(attempt.number) })}</strong><State value={attempt.state} />
            <div className="label">{attempt.started_at ? date(attempt.started_at) : t('waiting')}</div>
            {attempt.error && <div className="error">{t(`errors:${attempt.error}`)}</div>}
            {attempt.evidence && <small className="evidence" dir="auto">{attempt.evidence}</small>}
          </div>)}
        </Panel>
        <PublicationPanel run={run} publicationCommand={publicationCommand} />
      </div>
      <div>
        <Panel title={t('information')}>
          {[[t('identity'), run.run_id], [t('mode'), t(run.mode === 'fixture' ? 'fixture' : 'unverified')], [t('scenario'), run.input.mode === 'fixture' ? t(scenarioNames[run.input.scenario] ?? run.input.scenario) : '—'], [t('created'), date(run.created_at)], [t('updated'), date(run.updated_at)], [t('cursor'), number(run.last_event_id)]].map(([label, value]) => <div key={String(label)}><div className="label">{label}</div><div className="value" dir={label === t('identity') ? 'ltr' : 'auto'}>{value}</div></div>)}
        </Panel>
        <Panel title={t(run.mode === 'fixture' ? 'reports' : 'media')}>
          {run.component && <><bdi dir="ltr">{run.component.revision}</bdi><bdi dir="ltr">{run.component.verification}</bdi></>}
          {run.artifacts.map((artifact) => {
            const href = `/v1/artifacts/${encodeURIComponent(artifact.id)}/content`;
            return <div key={artifact.id}>
              {artifact.media_type === 'video/mp4' && <video controls preload="metadata" src={href} aria-label={t('openVideo')} />}
              <a className="report" target="_blank" rel="noopener" href={href}>{t(artifact.media_type === 'video/mp4' ? 'openVideo' : 'openReport')} · {t('bytes', { size: artifact.size })}</a>
              <bdi dir="ltr">SHA256 {artifact.sha256}</bdi>
            </div>;
          })}
          {!run.artifacts.length && <p>{t('noArtifact')}</p>}
        </Panel>
      </div>
    </div>
  </>;
}

function PublicationPanel({ run, publicationCommand }: { run: RunSnapshot; publicationCommand: (run: RunSnapshot, intentId: string | null, action: 'dispatch' | 'verify' | null, extra: Record<string, string>) => Promise<void> }) {
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const accountKey = `${run.run_id}:account`;
  const accountValue = typeof workspace.drafts.get(accountKey) === 'string' ? workspace.drafts.get(accountKey) as string : 'demo';
  const outcomes = [['success', 'confirmed'], ['submitted', 'submitted'], ['lost_response', 'lost'], ['not_sent', 'notSent'], ['slow', 'slow']] as const;
  return <Panel title={t('publication')}>
    <p>{t('subtitle')}</p>
    <label>{t('account')}<input value={accountValue} maxLength={64} dir="auto" onChange={(event) => workspace.setDraft(accountKey, event.currentTarget.value)} /></label>
    <button type="button" disabled={!run.allowed_actions.add_simulated_target.enabled || workspace.pending.get(`${run.run_id}:target`)?.busy} onClick={() => void publicationCommand(run, null, null, { platform: 'simulator', account: accountValue })}>{t('addTarget')}</button>
    {run.publications.map((intent) => {
      const scenarioKey = `${intent.id}:scenario`;
      const scenarioValue = typeof workspace.drafts.get(scenarioKey) === 'string' ? workspace.drafts.get(scenarioKey) as string : 'success';
      const actionKey = `${intent.id}:dispatch`;
      return <div className="attempt" data-intent-id={intent.id} key={intent.id}>
        <strong dir="auto">{intent.platform} / {intent.account}</strong><State value={intent.state} />
        <label>{t('outcome')}<select aria-label={t('outcome')} value={scenarioValue} onChange={(event) => workspace.setDraft(scenarioKey, event.currentTarget.value)}>
          {outcomes.map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
        </select></label>
        {(['dispatch', 'verify'] as const).map((action) => <button key={action} type="button" disabled={!intent.allowed_actions[action].enabled || workspace.pending.get(`${intent.id}:${action}`)?.busy} title={intent.allowed_actions[action].enabled ? t(action) : t('unknownHint')} onClick={() => void publicationCommand(run, intent.id, action, action === 'dispatch' ? { scenario: scenarioValue } : {})}>{t(action)}</button>)}
        {intent.state === 'UNKNOWN' && <p>{t('unknownHint')}</p>}
        {intent.attempts.map((attempt) => <small key={attempt.id}><bdi dir="ltr">{attempt.id}</bdi> · {t(`state:${attempt.state}`)}</small>)}
        {intent.receipts.map((receipt) => <p key={receipt.id}><bdi dir="ltr">{receipt.source}</bdi> · {t(`state:${receipt.outcome}`)} · <bdi dir="ltr">{receipt.work_id ?? '—'}</bdi><span className="evidence" dir="auto">{receipt.evidence}</span></p>)}
        {workspace.pending.has(actionKey) && <span className="muted" aria-live="polite">{t('syncing')}</span>}
      </div>;
    })}
  </Panel>;
}

function ErrorView({ code }: { code: string }) {
  const { t } = useTranslation();
  return <section className="empty"><h2>{t(code === 'NOT_FOUND' ? 'missing' : 'failedLoad')}</h2><p>{t(`errors:${code}`)}</p><Link className="back" href="/">{t('back')}</Link></section>;
}

function SettingsView() {
  const { t, i18n } = useTranslation();
  const workspace = useWorkspace();
  const preferences = currentPreferences();
  const [search, setSearch] = useState('');
  const names = useMemo(() => new Intl.DisplayNames([i18n.language], { type: 'language' }), [i18n.language]);
  const languageOptions = locales.filter((item) => {
    const text = `${item.name} ${names.of(item.code) ?? item.code}`;
    return item.code === preferences.language || text.toLocaleLowerCase(i18n.language).includes(search.toLocaleLowerCase(i18n.language));
  });
  const languageLabel = t('language');
  return <>
    <SectionHeading title={t('settings')} subtitle={t('preferencesHint')} />
    <div className="settings-brand"><img src="/brand.svg?size=48" width="48" height="48" alt="" aria-hidden="true" /><strong>LoopCast</strong></div>
    <div className="settings-grid">
      <Panel title={t('language')}>
        <label>{t('searchLanguage')}<input type="search" aria-label={t('searchLanguage')} placeholder={t('searchLanguage')} value={search} onChange={(event) => setSearch(event.currentTarget.value)} /></label>
        <label>{languageLabel}<select id="language-select" data-focus-key="language-select" value={preferences.language} onChange={(event) => window.workspacePreferences.set({ language: event.currentTarget.value })}>
          <option value="auto">{t('autoLanguage')}</option>
          {languageOptions.map((item) => <option key={item.code} value={item.code}>{item.name} · {names.of(item.code)}</option>)}
        </select></label>
        {workspace.packError && <p className="error">{t('packError')}</p>}
      </Panel>
      <Panel title={t('appearance')}>
        <label>{t('appearance')}<select id="theme-select" data-focus-key="theme-select" value={preferences.theme} onChange={(event) => window.workspacePreferences.set({ theme: event.currentTarget.value as Preferences['theme'] })}>
          {(['system', 'light', 'dark'] as const).map((value) => <option key={value} value={value}>{t(value)}</option>)}
        </select></label>
        {!workspace.preferencesAvailable && <p>{t('storageHint')}</p>}
      </Panel>
    </div>
    <Link className="back" href="/">{t('back')}</Link>
  </>;
}
