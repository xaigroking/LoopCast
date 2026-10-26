'use client';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest } from '@/lib/api';
import { useStudio, useStudioRead } from '@/lib/studio';
import type { Channel, ChannelList, ChannelReport, ChannelCheckResult, ChannelReportList, ChannelEventList } from '@/lib/inspection-client';
import { ErrorNotice, useFormat } from './studio-common';

function Report({ report }: { report: ChannelReport }) {
  const { t } = useTranslation(); const format = useFormat();
  return <article className="inspection-report" data-report={report.id}>
    <div className="section-head"><h4>{t('studio:inspectReport')}</h4><span className="state">{t('studio:inspectResult_' + report.result)}</span></div>
    <p>{t('studio:inspectFresh_' + report.freshness)} · {t('studio:inspectReason_' + report.reason_code, { defaultValue: t('studio:inspectReasonFallback') })}</p>
    <dl className="inspection-facts"><dt>{t('studio:inspectObserved')}</dt><dd><time dateTime={report.observed_at}>{format.date(report.observed_at)}</time></dd><dt>{t('studio:inspectValidUntil')}</dt><dd><time dateTime={report.valid_until}>{format.date(report.valid_until)}</time></dd><dt>{t('studio:inspectVersion')}</dt><dd>{format.number(report.channel_version)}{report.scope.account_version !== null && <> · {t('studio:inspectAccountVersion')} {format.number(report.scope.account_version)}</>}</dd><dt>{t('studio:inspectEvidence')}</dt><dd>{t('studio:inspectEvidence_' + report.evidence_status)} · {t('studio:inspectScope_' + report.evidence_scope)}</dd></dl>
    {report.scope.component_revision && <small className="evidence">{t('studio:inspectComponentRevision')} · <bdi>{report.scope.component_revision}</bdi></small>}
    {report.scope.artifact_sha256 && <small className="evidence">{t('studio:inspectArtifact')} · <bdi>{report.scope.artifact_sha256}</bdi></small>}
    <ul>{report.checks.map((check) => <li key={check.check_type}><strong>{t('studio:inspectCheck_' + check.check_type, { defaultValue: t('studio:inspectCheckFallback') })}</strong> · {t('studio:inspectResult_' + check.result)}<small>{t('studio:inspectSource_' + check.source.replaceAll('.', '_'))}</small>{check.value !== null && check.value !== undefined && <bdi>{typeof check.value === 'boolean' ? t(check.value ? 'studio:yes' : 'studio:no') : String(check.value)}</bdi>}</li>)}</ul>
    {report.declarations.length > 0 && <div className="inspection-declarations"><h5>{t('studio:inspectDeclarations')}</h5>{report.declarations.map((cap) => <p key={cap.operation}>{t('studio:inspectOperation_' + cap.operation)} · {t(cap.supported ? 'studio:inspectDeclared' : 'studio:inspectUnsupported')} · {t('studio:inspectEvidence_' + cap.evidence_status)}</p>)}</div>}
    <p className="inspection-proof">{t('studio:inspectForbiddenCounts', { generate: report.actual_operations.generate, model: report.actual_operations.paid_model, login: report.actual_operations.authorize, upload: report.actual_operations.upload })}</p>
    <small className="evidence">{t('studio:inspectReportIdentity')} · <bdi>{report.id}</bdi></small>
  </article>;
}

function ChannelCard({ channel }: { channel: Channel }) {
  const { t } = useTranslation(); const { client, perform, busy } = useStudio();
  const [online, setOnline] = useState(false); const [confirmed, setConfirmed] = useState(false); const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [history, setHistory] = useState<ChannelReportList | null>(null); const [events, setEvents] = useState<ChannelEventList | null>(null); const [selected, setSelected] = useState<ChannelReport | null>(null);
  const [error, setError] = useState<unknown>(null); const mounted = useRef(true); const requests = useRef(new Set<AbortController>());
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requests.current.forEach((c) => c.abort()); }; }, []);
  const identity = 'inspection-' + channel.id; const pending = [...client.pending.keys()].some((key) => key === identity);
  const currentVersion = selectedVersion ?? channel.channel_version; const conflict = currentVersion !== channel.channel_version;
  const read = async <T,>(path: string, apply: (value: T) => void) => {
    const controller = new AbortController(); requests.current.add(controller);
    try { const value = await apiRequest<T>(path, { signal: controller.signal }); if (mounted.current) { apply(value); setError(null); } }
    catch (e) { if (mounted.current && !controller.signal.aborted) setError(e); }
    finally { requests.current.delete(controller); }
  };
  const run = async () => {
    setError(null);
    try { await perform(identity, () => client.write<ChannelCheckResult>(identity, `/v1/operations/channels/${channel.id}/checks`, { expected_version: currentVersion, online, confirmed_online: online && confirmed })); if (mounted.current) setSelectedVersion(null); }
    catch (e) { if (mounted.current) setError(e); }
  };
  const heading = t('studio:inspectKind_' + channel.kind);
  return <article className="inspection-channel" data-channel={channel.id} data-kind={channel.kind}>
    <div className="section-head"><h3>{heading}</h3><span className="state">{channel.active_check ? t('studio:inspectState_' + channel.active_check.state) : t('studio:inspectFresh_' + channel.freshness)}</span></div>
    <p>{t('studio:inspectVersion')} {channel.channel_version}{channel.last_inventory_at && <> · {t('studio:inspectInventoryObserved')} <time dateTime={channel.last_inventory_at}>{channel.last_inventory_at.replace('T', ' ').replace('+00:00', ' UTC')}</time></>}</p>
    {channel.scope.account_id && <><p>{t('studio:inspectAccountReference')} · <bdi>{channel.scope.platform}</bdi> · <bdi>{channel.scope.account_id}</bdi></p><Link className="report" href="/accounts">{t('studio:inspectOpenAccount')}</Link></>}
    {channel.scope.evidence_mode === 'contract' && <p className="studio-alert">{t('studio:contractEvidence')}</p>}
    {channel.kind === 'ACCOUNT' && <label className="check"><input type="checkbox" checked={online} disabled={!channel.actions.online_check || pending || busy.has(identity)} onChange={(e) => { setOnline(e.target.checked); setConfirmed(false); setSelectedVersion(channel.channel_version); }} />{t('studio:inspectOnline')}</label>}
    {online && <label className="check"><input type="checkbox" checked={confirmed} disabled={pending || busy.has(identity)} onChange={(e) => setConfirmed(e.target.checked)} />{t('studio:inspectOnlineConsent')}</label>}
    {conflict && <p className="studio-alert" role="alert">{t('studio:inspectVersionChanged')} <button type="button" disabled={pending} onClick={() => { setSelectedVersion(null); setConfirmed(false); }}>{t('studio:inspectUseCurrent')}</button></p>}
    <div className="row-actions"><button type="button" data-action="inspect" disabled={!channel.actions.local_check || pending || busy.has(identity) || conflict || online && (!confirmed || !channel.actions.online_check)} onClick={() => void run()}>{t('studio:inspectRun')}</button><button type="button" onClick={() => void read<ChannelReportList>(`/v1/operations/channels/${channel.id}/reports`, setHistory)}>{t('studio:inspectHistory')}</button><button type="button" onClick={() => void read<ChannelEventList>(`/v1/operations/channel-events?channel_id=${channel.id}`, setEvents)}>{t('studio:inspectEvents')}</button></div>
    {channel.actions.retry_after_seconds > 0 && <p>{t('studio:inspectCooldown', { seconds: channel.actions.retry_after_seconds })}</p>}
    {Boolean(error) && <ErrorNotice error={error} retryPending={false} />}
    {channel.latest_report ? <Report report={channel.latest_report} /> : <p>{t('studio:inspectNotChecked')}</p>}
    {history && <section className="inspection-history" aria-label={t('studio:inspectHistory')}><h4>{t('studio:inspectHistory')}</h4>{history.items.length === 0 && <p>{t('studio:inspectNoHistory')}</p>}{history.items.map((report) => <Report report={report} key={report.id} />)}{history.next_cursor && <button type="button" onClick={() => void read<ChannelReportList>(`/v1/operations/channels/${channel.id}/reports?cursor=${encodeURIComponent(history.next_cursor!)}`, (page) => setHistory({ ...page, items: [...new Map([...history.items, ...page.items].map((item) => [item.id, item])).values()] }))}>{t('studio:inspectOlder')}</button>}<p>{t('studio:inspectRetention')}</p></section>}
    {events && <section className="inspection-events" aria-label={t('studio:inspectEvents')}><h4>{t('studio:inspectEvents')}</h4>{events.items.length === 0 && <p>{t('studio:inspectNoEvents')}</p>}{events.items.map((event) => <div className="attempt" key={event.id}><strong>{t('studio:inspectEvent_' + event.kind)}</strong><p>{t('studio:inspectReason_' + event.reason_code, { defaultValue: t('studio:inspectReasonFallback') })} · <time dateTime={event.created_at}>{event.created_at.replace('T', ' ').replace('+00:00', ' UTC')}</time></p><button type="button" onClick={() => void read<ChannelReport>(`/v1/operations/channel-reports/${event.current_report_id}`, setSelected)}>{t('studio:inspectViewReport')}</button>{event.previous_report_id && <button type="button" onClick={() => void read<ChannelReport>(`/v1/operations/channel-reports/${event.previous_report_id}`, setSelected)}>{t('studio:inspectViewPrevious')}</button>}</div>)}{events.next_cursor && <button type="button" onClick={() => void read<ChannelEventList>(`/v1/operations/channel-events?channel_id=${channel.id}&cursor=${encodeURIComponent(events.next_cursor!)}`, (page) => setEvents({ ...page, items: [...new Map([...events.items, ...page.items].map((item) => [item.id, item])).values()] }))}>{t('studio:inspectOlder')}</button>}<p>{t('studio:inspectRetention')}</p></section>}
    {selected && <div role="region" aria-label={t('studio:inspectSelectedReport')}><Report report={selected} /><button type="button" onClick={() => setSelected(null)}>{t('studio:inspectCloseReport')}</button></div>}
  </article>;
}

export function InspectionPanel() {
  const { t } = useTranslation(); const { client, perform, refresh, busy } = useStudio();
  const inventory = useStudioRead<ChannelList>('/v1/operations/channels', 3000);
  const [older, setOlder] = useState<Channel[]>([]); const [cursor, setCursor] = useState<string | null | undefined>(); const [error, setError] = useState<unknown>(null);
  const mounted = useRef(true); const request = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  const pending = [...client.pending.values()].filter((p) => p.path.startsWith('/v1/operations/channels/'));
  const items = [...(inventory.data?.items ?? []), ...older.filter((c) => !inventory.data?.items.some((recent) => recent.id === c.id))];
  const next = cursor === undefined ? inventory.data?.next_cursor : cursor;
  return <section id="channel-inspections" className="panel inspection-panel" aria-label={t('studio:inspectTitle')}><h2>{t('studio:inspectTitle')}</h2><p>{t('studio:inspectHint')}</p><p>{t('studio:inspectLiveBoundary')}</p>
    <p>{t(inventory.data?.policy.enabled ? 'studio:inspectPeriodicOn' : 'studio:inspectPeriodicOff')} · {t('studio:inspectNotificationsLocal')}</p>
    <button type="button" onClick={refresh}>{t('studio:inspectRefresh')}</button>
    {pending.length > 0 && <div role="alert" className="studio-alert"><p>{t('studio:inspectPending')}</p>{pending.map((operation) => <button type="button" key={operation.identity} disabled={busy.has(operation.identity)} onClick={() => { void perform(operation.identity, () => client.write(operation.identity, operation.path, operation.body, operation.method)).catch(setError); }}>{t('studio:inspectRetrySame')}</button>)}</div>}
    {Boolean(inventory.error || error) && <ErrorNotice error={inventory.error || error} retryPending={false} />}
    {!inventory.data ? <p>{t('studio:loading')}</p> : items.map((channel) => <ChannelCard channel={channel} key={channel.id} />)}
    {next && <button type="button" onClick={() => { request.current?.abort(); const controller = new AbortController(); request.current = controller; void apiRequest<ChannelList>(`/v1/operations/channels?cursor=${encodeURIComponent(next)}`, { signal: controller.signal }).then((page) => { if (mounted.current) { setOlder((current) => [...new Map([...current, ...page.items].map((item) => [item.id, item])).values()]); setCursor(page.next_cursor); } }).catch((e) => { if (mounted.current && !controller.signal.aborted) setError(e); }); }}>{t('studio:inspectOlder')}</button>}
  </section>;
}
