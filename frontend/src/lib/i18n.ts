'use client';

import i18next, { type i18n as I18n } from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';
import registry from '../../public/locales/registry.json';
import english from '../../public/locales/en.json';
import { currentPreferences } from './preferences';
import { requestJSON } from './http';

export type LocaleItem = (typeof registry)[number];
export const locales = registry as LocaleItem[];
const codes = locales.map((item) => item.code);
const engine: I18n = i18next.createInstance();
let initialization: Promise<void> | undefined;

export function canonicalLocale(tag: string): string {
  try {
    const locale = new Intl.Locale(tag.replaceAll('_', '-'));
    if (locale.language === 'zh') {
      return locale.script === 'Hant' || (!locale.script && ['TW', 'HK', 'MO'].includes(locale.region ?? ''))
        ? 'zh-Hant'
        : 'zh-Hans';
    }
    if (locale.language === 'pt') return 'pt-BR';
    return codes.includes(locale.language) ? locale.language : 'en';
  } catch {
    return 'en';
  }
}

async function loadPack(code: string) {
  const { response, data } = await requestJSON(`/locales/${encodeURIComponent(code)}.json`, { cache: 'force-cache' });
  if (!response.ok) throw new Error(`Language pack ${code} failed with ${response.status}`);
  if (!data) throw new Error('Language pack is invalid');
  return data as typeof english;
}

function detectLanguage(): string {
  const preference = currentPreferences().language;
  if (codes.includes(preference)) return preference;
  const detected = engine.services.languageDetector?.detect();
  const candidates: string[] = Array.isArray(detected) ? [...detected] : detected ? [detected] : [];
  if (typeof navigator !== 'undefined') candidates.push(...navigator.languages);
  for (const candidate of candidates) {
    const code = canonicalLocale(candidate);
    if (codes.includes(code)) return code;
  }
  return 'en';
}

async function installPack(code: string) {
  if (engine.hasResourceBundle(code, 'ui')) return;
  const pack = await loadPack(code);
  for (const namespace of ['ui', 'state', 'errors'] as const) {
    engine.addResourceBundle(code, namespace, pack[namespace], true, true);
  }
}

export async function initializeLanguage(): Promise<{ language: string; packError: boolean }> {
  if (!initialization) {
    initialization = (async () => {
      await engine
        .use(LanguageDetector)
        .use(initReactI18next)
        .init({
          lng: 'en',
          fallbackLng: 'en',
          supportedLngs: codes,
          load: 'currentOnly',
          resources: { en: english },
          ns: ['ui', 'state', 'errors'],
          defaultNS: 'ui',
          interpolation: { escapeValue: false },
          returnNull: false,
          returnEmptyString: false,
          detection: {
            order: ['navigator'],
            caches: [],
            convertDetectedLanguage: canonicalLocale,
          },
        });
    })();
  }
  await initialization;
  return changeLanguage(detectLanguage());
}

let languageEpoch = 0;
export async function changeLanguage(code: string): Promise<{ language: string; packError: boolean }> {
  const epoch = ++languageEpoch;
  let packError = false;
  let selected = code === 'auto' ? detectLanguage() : codes.includes(code) ? code : 'en';
  try {
    await installPack(selected);
  } catch {
    selected = 'en';
    packError = code !== 'en';
  }
  if (epoch !== languageEpoch) return { language: engine.language ?? 'en', packError: false };
  await engine.changeLanguage(selected);
  document.documentElement.lang = selected;
  document.documentElement.dir = engine.dir(selected);
  return { language: selected, packError };
}

export { engine };
