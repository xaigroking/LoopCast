'use client';
import { useTranslation } from 'react-i18next';
import { ApiError } from '@/lib/api';
import { useStudio } from '@/lib/studio';
import type { Asset } from '@/lib/studio-client';
import { assetURL } from '@/lib/studio-client';

export function StateBadge({ value }: { value: string }) {
  const { t } = useTranslation();
  return <span className="state" data-state={value}>{t(`studio:state_${value}`, { defaultValue: value })}</span>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  const { t } = useTranslation();
  const { client, perform } = useStudio();
  const code = error instanceof ApiError ? error.code : error instanceof Error ? error.message : 'INTERNAL_ERROR';
  const key = code === 'VERSION_CONFLICT' ? 'conflict' : code === 'REQUEST_UNRESOLVED' ? 'pendingAction' : code.includes('CAPABILITY') || code.includes('COMPONENT') ? 'missingConfig' : code.includes('MEDIA') || code.includes('ARTIFACT') ? 'invalidMedia' : 'failed';
  return <div className="studio-alert" role="alert"><span>{t(`studio:${key}`)}</span>
    {error instanceof ApiError && error.requestId && <small><bdi>{error.requestId}</bdi></small>}
    {client.pending.size > 0 && <button type="button" onClick={() => void perform('pending', () => client.retry()).catch(() => undefined)}>{t('studio:retryPending')}</button>}
  </div>;
}

export function CapabilityNotice() {
  const { t } = useTranslation(); const { caps, error } = useStudio();
  if (error) return <ErrorNotice error={error} />;
  if (!caps) return <p role="status">{t('studio:loading')}</p>;
  return caps.evidence_mode === 'contract' ? <p className="studio-alert" role="status">{t('studio:contractMode')}</p> : null;
}

export function AssetPreview({ asset, compact = false }: { asset: Asset; compact?: boolean }) {
  const { t } = useTranslation();
  if (asset.kind === 'video') return <video className={compact ? 'media-thumb' : 'media-preview'} src={assetURL(asset.id)} controls={!compact} preload="metadata" playsInline aria-label={asset.name} />;
  if (asset.kind === 'audio') return <audio src={assetURL(asset.id)} controls preload="metadata" aria-label={asset.name} />;
  if (asset.kind === 'image') return <img className={compact ? 'media-thumb' : 'media-preview'} src={assetURL(asset.id)} alt={asset.name} />;
  return <a className="report" href={assetURL(asset.id)} download>{t('studio:download')} · <bdi>{asset.name}</bdi></a>;
}

export function useFormat() {
  const { i18n } = useTranslation();
  return {
    number: (value: number) => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 }).format(value),
    duration: (value: number) => new Intl.NumberFormat(i18n.language, { style: 'unit', unit: 'second', maximumFractionDigits: 1 }).format(value),
    date: (value: string) => new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)),
  };
}
