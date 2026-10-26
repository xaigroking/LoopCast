'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { locales } from '@/lib/i18n';
import { useStudio } from '@/lib/studio';
import { useWorkspace } from '@/lib/workspace';
import { CreateDialog } from './create-dialog';

export function Brand({ size = 32, className = '' }: { size?: number; className?: string }) {
  return <img className={`brand-image ${className}`} src={`/brand.svg?size=${size}`} width={size} height={size} alt="" aria-hidden="true" />;
}

export function WorkspaceShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const workspace = useWorkspace();
  const studio = useStudio();
  const pathname = usePathname();
  const language = locales.find((item) => item.code === workspace.language)?.name ?? 'English';
  return <>
    <aside>
      <Link className="brand" href="/"><Brand size={38} /><span>LoopCast</span></Link>
      <span className="eyebrow">{t('workspace')}</span>
      <nav>
        <Link className="nav" href="/" aria-current={pathname === '/' ? 'page' : undefined}>{t('studio:workbench')}</Link>
        <Link className="nav" href="/create" aria-current={pathname.startsWith('/create') ? 'page' : undefined}>{t('studio:create')}</Link>
        <Link className="nav" href="/library" aria-current={pathname === '/library' ? 'page' : undefined}>{t('studio:library')}</Link>
        <Link className="nav" href="/accounts" aria-current={pathname === '/accounts' ? 'page' : undefined}>{t('studio:accounts')}</Link>
        <Link className="nav" href="/publish" aria-current={pathname === '/publish' || pathname === '/feedback' ? 'page' : undefined}>{t('studio:publish')}</Link>
        <Link className="nav" href="/settings" aria-current={pathname === '/settings' ? 'page' : undefined}>{t('settings')}</Link>
      </nav>
      <div className="aside-foot"><span className="dot" />{t('preferencesHint')}</div>
    </aside>
    <div className="workspace">
      <header>
        <Brand size={24} />
        <div className="badge">{pathname === '/' || pathname.startsWith('/runs/') ? t('fixtureBadge') : t('studio:workspace')}</div>
        <Link href="/settings" id="language-shortcut">{language}</Link>
        <button id="refresh" className="quiet" type="button" onClick={() => { workspace.refresh(); studio.refresh(); }}>{t('refresh')}</button>
        <span id="sync" role="status">{t(workspace.syncKey)}</span>
      </header>
      {workspace.packError && <p id="language-warning" className="resource-warning" role="status">{t('packError')}</p>}
      <main id="app" aria-live="polite">{children}</main>
      <footer><span>LoopCast</span><span>{t('studio:footer')}</span></footer>
    </div>
    {workspace.noticeCode && <div id="notice" role="alert"><span>{t(`errors:${workspace.noticeCode}`)}</span><button className="quiet" type="button" onClick={() => workspace.setNoticeCode(null)}>{t('dismiss')}</button></div>}
    <CreateDialog />
  </>;
}
