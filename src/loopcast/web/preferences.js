// Runs before stylesheets to resolve the initial appearance without a theme flash.
(() => {
  const defaults = {version: 1, language: 'auto', theme: 'system'};
  let value = {...defaults}, available = true;
  function decode(raw) {
    try {
      const p = JSON.parse(raw);
      if (p?.version !== 1 || typeof p.language !== 'string' || !['system','light','dark'].includes(p.theme)) return {...defaults};
      return {version: 1, language: p.language, theme: p.theme};
    } catch { return {...defaults}; }
  }
  try { value = decode(localStorage.getItem('loopcast.preferences')); } catch { available = false; }
  const media = matchMedia('(prefers-color-scheme: dark)');
  function apply() { document.documentElement.dataset.theme = value.theme === 'system' ? (media.matches ? 'dark' : 'light') : value.theme; }
  window.workspacePreferences = {
    get value() { return {...value}; }, get available() { return available; },
    set(patch) {
      value = decode(JSON.stringify({...value,...patch,version:1})); apply();
      try { localStorage.setItem('loopcast.preferences',JSON.stringify(value)); } catch { available = false; }
      window.dispatchEvent(new Event('preferenceschange'));
    }
  };
  media.addEventListener('change',apply);
  window.addEventListener('storage',event => {
    if (event.key === 'loopcast.preferences' || event.key === null) { value=decode(event.newValue); apply(); window.dispatchEvent(new Event('preferenceschange')); }
  });
  apply();
})();
