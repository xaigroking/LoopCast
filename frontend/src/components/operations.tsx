'use client';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest } from '@/lib/api';
import type { components } from '@/lib/api-schema';
import { useStudioRead } from '@/lib/studio';
import { platformName } from '@/lib/studio-client';
import { ErrorNotice, useFormat } from './studio-common';

type Status = components['schemas']['OperationsStatus'];
type Snapshot = components['schemas']['OperationsSnapshot'];
type Reconciliation = components['schemas']['ReconciliationReport'];

export function OperationsPanel({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation(); const format = useFormat();
  const status = useStudioRead<Status>('/v1/operations/status', 5000);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null); const [report, setReport] = useState<Reconciliation | null>(null);
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false);
  const load = (kind: 'snapshot' | 'reconciliation') => {
    setError(null); setBusy(true);
    const request = kind === 'snapshot'
      ? apiRequest<Snapshot>('/v1/operations/snapshot').then(setSnapshot)
      : apiRequest<Reconciliation>('/v1/operations/reconciliation').then(setReport);
    void request.catch(setError).finally(() => setBusy(false));
  };
  const value = status.data;
  return <section className="panel operations-panel" id="operations" aria-label={t('studio:operations')}>
    <h2>{t('studio:operations')}</h2>
    {Boolean(status.error || error) && <ErrorNotice error={status.error || error} />}
    {!value ? <p role="status">{t('studio:loading')}</p> : <>
      <div className="studio-grid"><p>{t('studio:serviceReadiness')} · <strong>{t(value.service_ready ? 'studio:serviceReady' : 'studio:serviceNotReady')}</strong></p><p>{t('studio:uploadPermission')} · <strong>{t(value.effective_sending_enabled ? 'studio:uploadsEnabled' : 'studio:uploadsBlocked')}</strong></p></div>
      <p>{t(value.evidence_mode === 'contract' ? 'studio:contractEvidence' : 'studio:liveEvidence')}</p>
      {value.restore_guard ? <div className="studio-alert restore-guard" role="status"><strong>{t('studio:restoreGuardActive')}</strong><p>{t('studio:restoreGuardHint')}</p><small className="evidence">{t('studio:restoreIdentity')} · <bdi>{value.restore_guard.restore_id}</bdi></small>{value.restore && <small>{t('studio:backupBoundary')} · {format.date(value.restore.backup_created_at)}</small>}</div> : <p className="restore-clear">{t('studio:restoreGuardClear')}</p>}
      {compact ? <Link className="report" href="/settings#operations">{t('studio:openOperations')}</Link> : <>
        <p>{t('studio:operationsHint')}</p><p>{t('studio:backupStatus')} · {value.backup ? format.date(value.backup.created_at) : t('studio:backupNotObserved')}</p>
        {value.backup && <small className="evidence">{t('studio:backupIdentity')} · <bdi>{value.backup.id}</bdi> · SHA-256 <bdi>{value.backup.manifest_sha256}</bdi></small>}
        <div className="row-actions"><button type="button" disabled={busy} onClick={() => load('snapshot')}>{t('studio:readSnapshot')}</button><button type="button" disabled={busy} onClick={() => load('reconciliation')}>{t('studio:readReconciliation')}</button></div>
        {snapshot && <div className="operations-snapshot" role="status"><h3>{t('studio:runtimeSnapshot')}</h3><p>{format.date(snapshot.observed_at)}</p><div className="operations-table"><table role="table"><thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">{t('studio:observationField')}</th><th role="columnheader" scope="col">{t('studio:observationValue')}</th><th role="columnheader" scope="col">{t('studio:observationStatus')}</th></tr></thead><tbody role="rowgroup">{(['application','runtime','database','media','generator','recovery'] as const).flatMap((group) => Object.entries(snapshot[group]).map(([key,item]) => <tr role="row" key={group + key}><th role="rowheader" scope="row">{t('studio:observation_' + key)}</th><td role="cell"><bdi>{item.value === null ? t('studio:observationUnavailable') : typeof item.value === 'object' ? Object.entries(item.value).map(([name,count]) => t('studio:count_' + name) + ': ' + format.number(count)).join(' · ') : typeof item.value === 'boolean' ? t(item.value ? 'studio:yes' : 'studio:no') : typeof item.value === 'string' && ['ACTIVE','CLEAR','held','not_held','COMPATIBLE_FORWARD_FIX_REQUIRED'].includes(item.value) ? t('studio:observationValue_' + item.value) : String(item.value)}</bdi></td><td role="cell">{t('studio:observationState_' + item.status)}</td></tr>))}</tbody></table></div><p>{t('studio:snapshotProvenanceHint')}</p></div>}
        {report && <div className="operations-reconciliation" role="status"><h3>{t('studio:reconciliationReport')}</h3><p>{t(report.coverage === 'NOT_RESTORED' ? 'studio:reconciliationNotRestored' : 'studio:reconciliationIncomplete')}</p><p>{t('studio:reconciliationNoRelease')}</p>{report.targets.map((target) => <div className="attempt" key={target.target_id}><strong>{platformName(target.platform)} · <bdi>{target.stable_subject}</bdi></strong><small className="evidence"><bdi>{target.target_id}</bdi></small><p>{t(target.known_publication === 'FOUND' ? 'studio:knownWorkFound' : 'studio:workInconclusive')} · {t(target.query === 'available' ? 'studio:targetQueryAvailable' : 'studio:targetQueryUnavailable')}</p><p>{t(target.evidence_mode === 'contract' ? 'studio:contractEvidence' : 'studio:liveEvidence')}</p></div>)}</div>}
      </>}
    </>}
  </section>;
}
