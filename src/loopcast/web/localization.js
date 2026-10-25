const engine = window.i18next;
const preferences = window.workspacePreferences;
export const registry = await fetch('/locales/registry.json').then(r => { if (!r.ok) throw new Error('registry'); return r.json(); });
const codes = registry.map(item => item.code);
const bundles = new Map();
export function canonicalLocale(tag) {
  try {
    const value = new Intl.Locale(tag.replaceAll('_','-'));
    if (value.language === 'zh') {
      if (value.script && !['Hans','Hant'].includes(value.script)) return tag;
      return value.script === 'Hant' || (!value.script && ['TW','HK','MO'].includes(value.region)) ? 'zh-Hant' : 'zh-Hans';
    }
    if (value.language === 'pt') return 'pt-BR';
    return codes.includes(value.language) ? value.language : tag;
  } catch { return tag; }
}
async function bundle(code) {
  if (!bundles.has(code)) bundles.set(code,fetch(`/locales/${code}.json`).then(response => {
    if (!response.ok) throw new Error('language pack'); return response.json();
  }).catch(error => { bundles.delete(code); throw error; }));
  return bundles.get(code);
}
const english = await bundle('en');
await engine.use(window.i18nextBrowserLanguageDetector).init({
  resources: {en: english}, ns:['ui','state','errors'], defaultNS:'ui',
  supportedLngs:codes, fallbackLng:'en', load:'currentOnly',
  interpolation:{escapeValue:false}, returnNull:false, returnEmptyString:false,
  detection:{order:['navigator'], caches:[], convertDetectedLanguage:canonicalLocale}
});
let epoch = 0;
export let packError = false;
export function t(key,options={}) {
  return engine.t(key,{defaultValue:engine.t('ui:requestError'),...options});
}
export function locale() { return document.documentElement.lang || 'en'; }
export function number(value) { return new Intl.NumberFormat(locale()).format(value); }
export function date(value) { return new Intl.DateTimeFormat(locale(),{dateStyle:'medium',timeStyle:'short'}).format(new Date(value)); }
export function errorText(error) { return t(`errors:${error?.code || 'INTERNAL_ERROR'}`); }
export async function updateLanguage() {
  const current = ++epoch;
  const manual = preferences.value.language;
  // Let the mature detector choose the first supported normalized browser language.
  const detected = engine.services.languageDetector.detect();
  const language = codes.includes(manual) ? manual : engine.services.languageUtils.getBestMatchFromCodes(detected) || 'en';
  let resource;
  try { resource = await bundle(language); } catch { resource = english; }
  if (current !== epoch) return;
  packError = resource === english && language !== 'en';
  const chosen = packError ? 'en' : language;
  for (const namespace of ['ui','state','errors']) engine.addResourceBundle(chosen,namespace,resource[namespace],true,true);
  await engine.changeLanguage(chosen);
  if (current !== epoch) return;
  document.documentElement.lang = chosen;
  document.documentElement.dir = engine.dir(chosen);
  window.dispatchEvent(new Event('localechange'));
}
window.addEventListener('preferenceschange',updateLanguage);
window.addEventListener('languagechange',() => { if (preferences.value.language === 'auto') updateLanguage(); });
await updateLanguage();
