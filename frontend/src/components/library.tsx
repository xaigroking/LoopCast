'use client';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useStudio, useStudioRead } from '@/lib/studio';
import { type Asset } from '@/lib/studio-client';
import { AssetPreview, CapabilityNotice, ErrorNotice, useFormat } from './studio-common';

export function LibraryPage() {
  const { t } = useTranslation(); const { client, caps, perform, busy } = useStudio(); const format = useFormat();
  const list = useStudioRead<{ items: Asset[] }>('/v1/studio/assets');
  const [file, setFile] = useState<File | null>(null); const [kind, setKind] = useState('video'); const [source, setSource] = useState(''); const [parent, setParent] = useState(''); const [error, setError] = useState<unknown>(null); const [filter, setFilter] = useState('');
  return <><div className="heading"><div><h1>{t('studio:library')}</h1><p>{t('studio:libraryHint')}</p></div><Link className="report" href="/create">{t('studio:create')}</Link></div><CapabilityNotice />{(error || list.error) && <ErrorNotice error={error || list.error} />}
    <form className="panel" onSubmit={(e) => { e.preventDefault(); if (!file) return; setError(null); void perform('import', async () => { await client.upload(file, kind, source, parent || null); setFile(null); }).catch(setError); }}><h2>{t('studio:importMedia')}</h2><div className="studio-grid"><label>{t('studio:file')}<input id="media-file" type="file" accept={kind === 'image' ? 'image/png,image/jpeg,image/webp' : kind === 'audio' ? 'audio/*' : 'video/mp4,video/webm'} onChange={(e) => setFile(e.target.files?.[0] ?? null)} required /></label><label>{t('studio:mediaKind')}<select value={kind} onChange={(e) => setKind(e.target.value)}>{['video','audio','image'].map((k) => <option key={k} value={k}>{t('studio:kind_' + k)}</option>)}</select></label><label>{t('studio:attribution')}<input id="media-source" value={source} onChange={(e) => setSource(e.target.value)} maxLength={500} required /></label><label>{t('studio:derivedFrom')}<select value={parent} onChange={(e) => setParent(e.target.value)}><option value="">{t('studio:newWork')}</option>{list.data?.items.filter((a) => a.kind === 'video').map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label></div><p>{t('studio:lineageHint')}</p><button className="primary" disabled={!file || !source.trim() || busy.has('import') || !caps?.media}>{t('studio:importMedia')}</button>{!caps?.media && <p>{t('studio:missingConfig')}</p>}</form>
    <label>{t('studio:search')}<input type="search" value={filter} onChange={(e) => setFilter(e.target.value)} /></label>
    {!list.data ? <p role="status">{t('studio:loading')}</p> : list.data.items.length === 0 ? <div className="empty">{t('studio:noAssets')}</div> : <div className="studio-grid">{list.data.items.filter((a) => a.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map((a) => <article className="panel asset-card" key={a.id}><AssetPreview asset={a} /><h2>{a.name}</h2><p>{t('studio:kind_' + a.kind)} · {format.number(a.size)} B{a.metadata.duration ? ' · ' + format.duration(a.metadata.duration) : ''}</p><small>{t('studio:attribution')} · <bdi>{a.source}</bdi></small><small className="evidence">SHA-256 · <bdi>{a.sha256}</bdi></small><small className="evidence">{t('studio:identity')} · <bdi>{a.family_id}</bdi></small>{a.kind === 'video' && <Link className="report" href={'/publish?asset=' + a.id}>{t('studio:handoff')}</Link>}</article>)}</div>}
  </>;
}
