'use client';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest } from '@/lib/api';
import type { components } from '@/lib/api-schema';
import { ErrorNotice } from './studio-common';

export function GeneratorStatus() {
  const { t } = useTranslation();
  const [report, setReport] = useState<components['schemas']['GeneratorDiagnostic'] | null>(null);
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false);
  return <section className="panel" id="generator-diagnostics"><h2>{t('studio:generatorSetup')}</h2><p>{t('studio:diagnosticHint')}</p><button disabled={busy} onClick={() => { setBusy(true); setError(null); void apiRequest<components['schemas']['GeneratorDiagnostic']>('/v1/studio/generator/diagnostics').then(setReport).catch(setError).finally(() => setBusy(false)); }}>{t('studio:runDiagnostics')}</button>{Boolean(error) && <ErrorNotice error={error} />}{report && <><p role="status">{t('studio:diagnostic_' + report.state)}</p><p>{t('studio:liveUnverified')}</p><ul>{report.checks.map((check) => <li key={check.id}><strong>{t('studio:check_' + check.id)}</strong> · {t('studio:check_' + check.state)}{check.code !== 'OK' && <p>{t('studio:diagnostic_code_' + check.code)}</p>}</li>)}</ul><p>{t('studio:diagnosticNetwork')}</p></>}</section>;
}
