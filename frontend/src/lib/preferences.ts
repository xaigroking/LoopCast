export type Preferences = { version: 1; language: string; theme: 'system' | 'light' | 'dark' };

declare global {
  interface Window {
    workspacePreferences: {
      value: Preferences;
      available: boolean;
      set: (patch: Partial<Preferences>) => void;
    };
  }
}

export function currentPreferences(): Preferences {
  return window.workspacePreferences?.value ?? { version: 1, language: 'auto', theme: 'system' };
}
