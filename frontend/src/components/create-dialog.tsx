'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent, type InvalidEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest, ApiError, requestKey, type RunCreate, type RunSnapshot } from '@/lib/api';
import { useWorkspace } from '@/lib/workspace';

export function CreateDialog() {
  const { t } = useTranslation();
  const workspace = useWorkspace();
  const router = useRouter();
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const operation = useRef<{ signature: string; key: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [retrySafe, setRetrySafe] = useState(false);
  const mode = String(workspace.drafts.get('create:mode') ?? 'fixture');
  const title = String(workspace.drafts.get('create:title') ?? '');
  const scenario = String(workspace.drafts.get('create:scenario') ?? 'success');
  const delay = String(workspace.drafts.get('create:delay') ?? '300');

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (workspace.createOpen && !element.open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      element.showModal();
      element.querySelector<HTMLInputElement>('input[name="title"]')?.focus();
    } else if (!workspace.createOpen && element.open) {
      element.close();
    }
  }, [workspace.createOpen]);

  function close() {
    setErrorCode(null);
    setRetrySafe(false);
    workspace.closeCreate();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const value: RunCreate = mode === 'mpt'
      ? { mode: 'mpt', title }
      : { mode: 'fixture', title, scenario: scenario as 'success' | 'fail_once' | 'fail' | 'slow', delay_ms: Number(delay) };
    const signature = JSON.stringify(value);
    try {
      if (!operation.current || operation.current.signature !== signature) operation.current = { signature, key: requestKey() };
    } catch {
      setErrorCode('SECURE_RANDOM_UNAVAILABLE');
      setRetrySafe(false);
      return;
    }
    setBusy(true);
    setErrorCode(null);
    setRetrySafe(false);
    void apiRequest<RunSnapshot>('/v1/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.current.key },
      body: signature,
    }).then((snapshot) => {
      workspace.accept(snapshot);
      operation.current = null;
      for (const key of ['create:title', 'create:mode', 'create:scenario', 'create:delay']) workspace.setDraft(key, key === 'create:scenario' ? 'success' : key === 'create:delay' ? '300' : '');
      workspace.resetCreate();
      router.push(`/runs/${snapshot.run_id}`);
    }).catch((error: unknown) => {
      if (error instanceof ApiError) {
        setErrorCode(error.code);
        setRetrySafe(false);
        if (error.snapshot) workspace.accept(error.snapshot);
      } else {
        setErrorCode(null);
        setRetrySafe(true);
      }
    }).finally(() => setBusy(false));
  }

  function invalid(event: InvalidEvent<HTMLFormElement>) {
    const element = event.target;
    if (element instanceof HTMLInputElement) element.setCustomValidity(t('invalid'));
  }

  const generating = mode === 'mpt';
  return <dialog ref={dialog} id="create-dialog" aria-labelledby="form-title" onClose={() => {
    if (workspace.createOpen) workspace.closeCreate();
    const target = opener.current?.isConnected ? opener.current : document.querySelector<HTMLElement>('.heading .primary');
    target?.focus({ preventScroll: true });
  }}>
    <form id="create-form" onSubmit={submit} onInvalid={invalid}>
      <div className="eyebrow">{t('workspace')}</div>
      <h2 id="form-title">{t(generating ? 'generate' : 'new')}</h2>
      <p id="form-description">{generating ? `${t('media')} · ${t('unverified')}` : t('fixtureNotice')}</p>
      <label>{t('mode')}<select name="mode" value={mode} onChange={(event) => workspace.setDraft('create:mode', event.currentTarget.value)}>
        <option value="fixture">{t('fixture')}</option>
        <option value="mpt" disabled={!workspace.generatorAvailable}>{t('media')} · {t(workspace.generatorAvailable ? 'unverified' : 'unavailable')}</option>
      </select></label>
      <label>{t('name')}<input name="title" required maxLength={120} autoComplete="off" dir="auto" value={title} onChange={(event) => { event.currentTarget.setCustomValidity(''); workspace.setDraft('create:title', event.currentTarget.value); }} /></label>
      {!generating && <>
        <label>{t('scenario')}<select name="scenario" value={scenario} onChange={(event) => workspace.setDraft('create:scenario', event.currentTarget.value)}>
          {([['success', 'success'], ['fail_once', 'failOnce'], ['fail', 'fail'], ['slow', 'slow']] as const).map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
        </select></label>
        <label>{t('delay')}<input name="delay_ms" type="number" min={0} max={3000} step={1} required value={delay} onChange={(event) => { event.currentTarget.setCustomValidity(''); workspace.setDraft('create:delay', event.currentTarget.value); }} /></label>
      </>}
      <p id="form-error" className="error" role="alert">{errorCode ? t(`errors:${errorCode}`) : retrySafe ? t('retrySafe') : ''}</p>
      <div className="dialog-actions">
        <button type="button" id="dialog-settings" onClick={() => { close(); router.push('/settings'); }}>{t('settings')}</button>
        <button type="button" id="dismiss" className="quiet" onClick={close}>{t('dismiss')}</button>
        <button type="submit" className="primary" disabled={busy}>{t(busy ? 'creating' : generating ? 'generate' : 'create')}</button>
      </div>
    </form>
  </dialog>;
}
