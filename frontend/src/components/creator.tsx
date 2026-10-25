'use client';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { apiRequest, assetText, requestKey, type RunSnapshot } from '@/lib/api';
import { subscribeEvents } from '@/lib/events';
import { useStudio, useStudioRead } from '@/lib/studio';
import { assetURL, draftBuffer, type Project, type Draft, type Asset, type Scene } from '@/lib/studio-client';
import { AssetPreview, CapabilityNotice, ErrorNotice, StateBadge, useFormat } from './studio-common';

export function ProjectsPage() {
  const { t } = useTranslation(); const router = useRouter(); const { client, perform, busy } = useStudio();
  const list = useStudioRead<{ items: Project[] }>('/v1/studio/projects');
  const [title, setTitle] = useState(''); const [error, setError] = useState<unknown>(null); const format = useFormat();
  return <><div className="heading"><div><span className="eyebrow">LoopCast</span><h1>{t('studio:create')}</h1><p>{t('studio:createHint')}</p></div><Link className="report" href="/library">{t('studio:library')}</Link></div>
    <CapabilityNotice />{(error || list.error) && <ErrorNotice error={error || list.error} />}
    <form className="panel inline-form" onSubmit={(e) => { e.preventDefault(); void perform('project-create', async () => {
      const project = await client.write<Project>('project-create', '/v1/studio/projects', { title }); router.push('/create/' + project.id);
    }).catch(setError); }}><label>{t('studio:title')}<input id="project-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} required /></label><button className="primary" disabled={busy.has('project-create')}>{t('studio:newProject')}</button></form>
    {!list.data ? <p role="status">{t('studio:loading')}</p> : list.data.items.length === 0 ? <div className="empty"><h2>{t('studio:noProjects')}</h2><p>{t('studio:createHint')}</p></div> : <div className="studio-grid">{list.data.items.map((project) => <Link className="panel project-card" key={project.id} href={'/create/' + project.id}><h2>{project.draft.title}</h2><p>{project.draft.brief}</p><small>{format.date(project.updated_at)} · {t('studio:revisions')} {format.number(project.revisions.length)}</small></Link>)}</div>}
  </>;
}

export function CreatorPage() {
  const { project_id } = useParams<{ project_id: string }>(); const { t } = useTranslation();
  const { client, caps, perform, busy, refresh } = useStudio(); const format = useFormat();
  const remote = useStudioRead<Project>('/v1/studio/projects/' + project_id, 2500);
  const library = useStudioRead<{ items: Asset[] }>('/v1/studio/assets', 4000);
  const [draft, setDraft] = useState<Draft | null>(null); const [baseVersion, setBaseVersion] = useState(0);
  const [error, setError] = useState<unknown>(null); const [storageIssue, setStorageIssue] = useState(false);
  const [revisionId, setRevisionId] = useState(''); const [run, setRun] = useState<RunSnapshot | null>(null);
  const [time, setTime] = useState(0); const [duration, setDuration] = useState(0); const video = useRef<HTMLVideoElement>(null);
  const initialized = useRef('');
  useEffect(() => {
    if (remote.data && initialized.current !== project_id) {
      const cached = draftBuffer(remote.data); setDraft(cached.draft); setBaseVersion(cached.baseVersion); setStorageIssue(!cached.available); initialized.current = project_id;
    }
  }, [remote.data, project_id]);
  useEffect(() => {
    if (!draft || initialized.current !== project_id) return;
    try { sessionStorage.setItem('loopcast.studio.draft.' + project_id, JSON.stringify({ draft, baseVersion })); } catch { setStorageIssue(true); }
  }, [draft, baseVersion, project_id]);
  const project = remote.data;
  const revision = project?.revisions.find((r) => r.id === revisionId) ?? project?.revisions[0];
  const generations = revision?.generations ?? [];
  const active = generations.find((g) => ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(g.state));
  useEffect(() => {
    if (!active) return;
    let cursor = 0;
    const subscription = subscribeEvents({ url: () => `/v1/runs/${active.run_id}/events?after=${cursor}`,
      receive: (event) => { const value = JSON.parse(event.data) as RunSnapshot; cursor = value.last_event_id; setRun(value); if (['SUCCEEDED','FAILED','CANCELLED'].includes(value.state)) { refresh(); return true; } return false; }, state: () => undefined });
    return () => subscription.close();
  }, [active?.run_id, refresh]);
  useEffect(() => {
    if (remote.data && draft && baseVersion < remote.data.version && JSON.stringify(draft) === JSON.stringify(remote.data.draft)) setBaseVersion(remote.data.version);
  }, [remote.data, draft, baseVersion]);
  // A run stream only updates execution facts; the editable buffer keeps its own version.
  if (!draft || !project) return <>{remote.error ? <ErrorNotice error={remote.error} /> : <p role="status">{t('studio:loading')}</p>}</>;
  const assets = library.data?.items ?? []; const assetMap = new Map(assets.map((a) => [a.id, a]));
  const outputs = [...new Set(generations.flatMap((g) => g.asset_ids))].map((id) => assetMap.get(id)).filter((a): a is Asset => Boolean(a));
  const selectedVideo = outputs.find((a) => a.kind === 'video');
  const changed = JSON.stringify(draft) !== JSON.stringify(project.draft);
  const update = (value: Partial<Draft>) => setDraft({ ...draft, ...value });
  const settings = (value: Partial<Draft['settings']>) => update({ settings: { ...draft.settings, ...value } });
  const scenes = (items: Scene[]) => update({ scenes: items, script: items.map((s) => s.narration).join('\n\n') });
  const save = async () => {
    const saved = await client.write<Project>('save-' + project_id, '/v1/studio/projects/' + project_id, { expected_version: baseVersion, draft }, 'PUT');
    setBaseVersion(saved.version); setDraft((current) => JSON.stringify(current) === JSON.stringify(draft) ? saved.draft : current); return saved;
  };
  const execute = (id: string, action: () => Promise<unknown>) => { setError(null); void perform(id, action).catch(setError); };
  const generate = (stage: 'script' | 'audio' | 'materials' | 'subtitle' | 'video') => execute('generation', async () => {
    let saved = project;
    if (changed || baseVersion !== project.version) saved = await save();
    let frozen = saved.revisions[0];
    if (!frozen || JSON.stringify(frozen.draft) !== JSON.stringify(saved.draft)) {
      saved = await client.write<Project>('freeze-' + project_id, `/v1/studio/projects/${project_id}/revisions`, { expected_version: saved.version }); frozen = saved.revisions[0];
    }
    if (!frozen) throw Error('INTERNAL_ERROR');
    const generated = await client.write<Project>('generate-' + project_id, `/v1/studio/projects/${project_id}/revisions/${frozen.id}/generate`, { expected_version: saved.version, stage });
    setBaseVersion(generated.version); setRevisionId(frozen.id); remote.reload();
  });
  return <><Link className="back" href="/create">← {t('studio:create')}</Link><div className="heading"><div><h1>{draft.title}</h1><p>{t('studio:draftStatus')} · {t(changed ? 'studio:unsaved' : 'studio:saved')}</p></div><div className="row-actions"><button onClick={() => execute('save', save)} disabled={busy.size > 0}>{t('studio:save')}</button><button onClick={() => execute('freeze', async () => { const saved = changed ? await save() : project; const frozen = await client.write<Project>('freeze-' + project_id, `/v1/studio/projects/${project_id}/revisions`, { expected_version: saved.version }); setBaseVersion(frozen.version); setRevisionId(frozen.revisions[0]?.id ?? ''); })} disabled={busy.size > 0}>{t('studio:freeze')}</button></div></div>
    <CapabilityNotice />{(error || remote.error || library.error) && <ErrorNotice error={error || remote.error || library.error} />}{(storageIssue || !client.storageAvailable) && <p role="alert" className="studio-alert">{t('studio:storageIssue')}</p>}
    {baseVersion !== project.version && <div className="studio-alert" role="alert">{t('studio:conflict')} <button onClick={() => { setDraft(project.draft); setBaseVersion(project.version); }}>{t('studio:loadSaved')}</button></div>}
    <div className="studio-steps"><a href="#script">1 · {t('studio:scriptScenes')}</a><a href="#configure">2 · {t('studio:voiceMaterials')}</a><a href="#preview">3 · {t('studio:previewVersions')}</a></div>
    <div className="creation-grid"><div><section id="script" className="panel"><h2>{t('studio:scriptScenes')}</h2><label>{t('studio:title')}<input value={draft.title} onChange={(e) => update({ title: e.target.value })} maxLength={120} required /></label><label>{t('studio:brief')}<textarea value={draft.brief} onChange={(e) => update({ brief: e.target.value })} maxLength={2000} rows={3} /></label><label>{t('studio:script')}<textarea id="creation-script" value={draft.script} onChange={(e) => update({ script: e.target.value, scenes: [] })} maxLength={20000} rows={7} /></label>
      <div className="row-actions"><button onClick={() => generate('script')} disabled={busy.size > 0 || !caps?.script_generation}>{t('studio:generateScript')}</button><button onClick={() => scenes(draft.script.split(/\n\s*\n/).filter((text) => text.trim()).map((text) => ({ id: requestKey().replaceAll('-', ''), narration: text, visual: '', duration: 5 })))} disabled={!draft.script.trim()}>{t('studio:splitScenes')}</button></div>
      <p className="muted">{t('studio:sceneHint')}</p>{draft.scenes.map((scene, index) => <fieldset className="scene" key={scene.id}><legend>{t('studio:scene')} {format.number(index + 1)}</legend><label>{t('studio:narration')}<textarea value={scene.narration} maxLength={4000} rows={3} onChange={(e) => scenes(draft.scenes.map((s) => s.id === scene.id ? { ...s, narration: e.target.value } : s))} /></label><label>{t('studio:visual')}<input value={scene.visual} maxLength={1000} onChange={(e) => scenes(draft.scenes.map((s) => s.id === scene.id ? { ...s, visual: e.target.value } : s))} /></label><label>{t('studio:duration')}<input type="number" min="0.1" max="300" step="0.1" value={scene.duration} onChange={(e) => scenes(draft.scenes.map((s) => s.id === scene.id ? { ...s, duration: Number(e.target.value) } : s))} /></label><div className="row-actions">{[-1, 1].map((direction) => <button key={direction} aria-label={t(direction < 0 ? 'studio:moveUp' : 'studio:moveDown')} disabled={index + direction < 0 || index + direction >= draft.scenes.length} onClick={() => { const next = [...draft.scenes]; [next[index], next[index + direction]] = [next[index + direction], next[index]]; scenes(next); }}>{direction < 0 ? '↑' : '↓'}</button>)}<button onClick={() => scenes([...draft.scenes.slice(0, index + 1), { ...scene, id: requestKey().replaceAll('-', '') }, ...draft.scenes.slice(index + 1)])}>{t('studio:duplicate')}</button><button onClick={() => scenes(draft.scenes.filter((s) => s.id !== scene.id))}>{t('studio:remove')}</button></div></fieldset>)}
      <button onClick={() => scenes([...draft.scenes, { id: requestKey().replaceAll('-', ''), narration: '', visual: '', duration: 5 }])} disabled={draft.scenes.length >= 100}>{t('studio:addScene')}</button><p>{t('studio:totalDuration')} · {format.duration(draft.scenes.reduce((n, s) => n + s.duration, 0))}</p>
    </section>
    <section id="configure" className="panel"><h2>{t('studio:voiceMaterials')}</h2><div className="studio-grid"><label>{t('studio:language')}<input value={draft.settings.language} maxLength={20} onChange={(e) => settings({ language: e.target.value })} /></label><label>{t('studio:voice')}<select value={draft.settings.voice} onChange={(e) => settings({ voice: e.target.value })}>{(caps?.voices ?? ['no-voice']).map((voice) => <option key={voice} value={voice}>{voice === 'no-voice' ? t('studio:noVoice') : voice}</option>)}</select></label><label>{t('studio:voiceRate')}<input type="number" min="0.5" max="2" step="0.1" value={draft.settings.voice_rate} onChange={(e) => settings({ voice_rate: Number(e.target.value) })} /></label><label>{t('studio:aspect')}<select value={draft.settings.aspect} onChange={(e) => settings({ aspect: e.target.value as Draft['settings']['aspect'] })}>{['9:16','16:9','1:1'].map((value) => <option key={value}>{value}</option>)}</select></label></div><button onClick={() => generate('audio')} disabled={busy.size > 0 || !caps?.generation}>{t('studio:previewVoice')}</button>
      <label>{t('studio:source')}<select value={draft.settings.source} onChange={(e) => settings({ source: e.target.value as Draft['settings']['source'] })}>{(caps?.sources ?? ['local']).map((value) => <option key={value} value={value}>{value === 'local' ? t('studio:localMedia') : value}</option>)}</select></label><Link className="report" href="/library">{t('studio:importMedia')}</Link><fieldset><legend>{t('studio:materials')}</legend>{assets.filter((a) => ['video','image'].includes(a.kind)).map((asset) => <label className="check" key={asset.id}><input type="checkbox" checked={draft.settings.material_ids.includes(asset.id)} onChange={(e) => settings({ material_ids: e.target.checked ? [...draft.settings.material_ids, asset.id] : draft.settings.material_ids.filter((id) => id !== asset.id) })} /><span>{asset.name}</span></label>)}{!assets.some((a) => ['video','image'].includes(a.kind)) && <p>{t('studio:noAssets')}</p>}</fieldset>
      {(['audio_id','music_id'] as const).map((field) => <label key={field}>{t(field === 'audio_id' ? 'studio:customAudio' : 'studio:music')}<select value={draft.settings[field] ?? ''} onChange={(e) => settings({ [field]: e.target.value || null })}><option value="">{t('studio:none')}</option>{assets.filter((a) => a.kind === 'audio').map((a) => <option value={a.id} key={a.id}>{a.name}</option>)}</select></label>)}<label>{t('studio:musicVolume')}<input type="range" min="0" max="1" step="0.05" value={draft.settings.music_volume} onChange={(e) => settings({ music_volume: Number(e.target.value) })} /></label><label className="check"><input type="checkbox" checked={draft.settings.subtitles} onChange={(e) => settings({ subtitles: e.target.checked })} />{t('studio:subtitles')}</label>
      <details><summary>{t('studio:advanced')}</summary><div className="studio-grid"><label>{t('studio:subtitlePosition')}<select value={draft.settings.subtitle_position} onChange={(e) => settings({ subtitle_position: e.target.value as Draft['settings']['subtitle_position'] })}>{['top','center','bottom'].map((value) => <option key={value} value={value}>{t('studio:' + value)}</option>)}</select></label><label>{t('studio:subtitleSize')}<input type="number" min="12" max="120" value={draft.settings.subtitle_size} onChange={(e) => settings({ subtitle_size: Number(e.target.value) })} /></label><label>{t('studio:clipDuration')}<input type="number" min="1" max="30" value={draft.settings.clip_duration} onChange={(e) => settings({ clip_duration: Number(e.target.value) })} /></label><label>{t('studio:keywords')}<input value={draft.settings.keywords.join(', ')} onChange={(e) => settings({ keywords: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} /></label><label>{t('studio:derivedFrom')}<select value={draft.settings.parent_video_id ?? ''} onChange={(e) => settings({ parent_video_id: e.target.value || null })}><option value="">{t('studio:newWork')}</option>{assets.filter((a) => a.kind === 'video').map((a) => <option value={a.id} key={a.id}>{a.name}</option>)}</select></label></div></details>
    </section></div><section id="preview" className="panel creator-preview"><h2>{t('studio:previewVersions')}</h2><label>{t('studio:revisions')}<select value={revision?.id ?? ''} onChange={(e) => setRevisionId(e.target.value)}><option value="" disabled>{t('studio:none')}</option>{project.revisions.map((r) => <option key={r.id} value={r.id}>{format.number(r.number)} · {format.date(r.created_at)}</option>)}</select></label>
      {!caps?.generation && <p className="studio-alert">{t('studio:missingConfig')}</p>}<div className="row-actions">{(['materials','subtitle','video'] as const).map((stage) => <button className={stage === 'video' ? 'primary' : ''} key={stage} disabled={busy.size > 0 || Boolean(active) || !caps?.generation} onClick={() => generate(stage)}>{t('studio:generate_' + stage)}</button>)}</div>
      {generations.map((g) => <div className="generation" key={g.id}><span>{t('studio:stage_' + g.stage)}</span><StateBadge value={g.state} /><Link href={'/runs/' + g.run_id}>{t('studio:execution')}</Link><small><bdi>{g.component_revision}</bdi></small>{['QUEUED','RUNNING'].includes(g.state) && <button onClick={() => execute('cancel-generation', async () => { const current = run?.run_id === g.run_id ? run : await apiRequest<RunSnapshot>('/v1/runs/' + g.run_id); return client.write('cancel-' + g.run_id, `/v1/runs/${g.run_id}/cancel`, { expected_version: current.state_version }); })}>{t('studio:cancel')}</button>}</div>)}
      {selectedVideo && <div className="real-video"><video key={selectedVideo.id} ref={video} controls playsInline preload="metadata" src={assetURL(selectedVideo.id)} onLoadedMetadata={(e) => { setDuration(e.currentTarget.duration); setTime(0); }} onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)} /><label>{t('studio:timeline')}<input type="range" min="0" max={duration || 0} step="0.05" value={time} onChange={(e) => { if (video.current) video.current.currentTime = Number(e.target.value); setTime(Number(e.target.value)); }} /></label><p>{format.duration(time)} / {format.duration(duration)}</p><Link className="report" href={'/publish?asset=' + selectedVideo.id}>{t('studio:handoff')}</Link></div>}
      {outputs.length === 0 && <p className="empty">{t('studio:noOutputs')}</p>}{outputs.map((asset) => <div className="output" key={asset.id}>{asset.kind !== 'video' && <AssetPreview asset={asset} />}<small>{asset.name} · <bdi>{asset.sha256}</bdi></small><a className="report" href={assetURL(asset.id)} download>{t('studio:download')}</a>{asset.kind === 'script' && <button onClick={() => execute('adopt-script', async () => { const text = await assetText(asset.id); update({ script: text, scenes: [] }); })}>{t('studio:useScript')}</button>}</div>)}
    </section></div></>;
}
