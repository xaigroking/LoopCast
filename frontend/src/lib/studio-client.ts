import { apiRequest, ApiError, requestKey } from './api';
import { requestJSON } from './http';
import type { components } from './api-schema';

export type Project = components['schemas']['Project'];
export type Draft = Project['draft'];
export type Scene = Draft['scenes'][number];
export type Asset = components['schemas']['Asset'];
export type Account = components['schemas']['Account'];
export type Authorization = components['schemas']['Authorization'];
export type Capabilities = components['schemas']['StudioCapabilities'];
export type Platform = components['schemas']['PlatformCapability']['platform'];
export type PlatformFields = Release['targets'][number]['fields'];
export type PreflightInput = components['schemas']['PreflightRequest'];
export type Preflight = components['schemas']['Preflight'];
export type Release = components['schemas']['ReleaseBatch'];
export type Interaction = components['schemas']['Interaction'];

type Pending = { identity: string; path: string; method: 'POST' | 'PUT'; body: unknown; key: string; sensitive: boolean };
type StoragePort = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const storageKey = 'loopcast.studio.pending.v1';

export class StudioClient {
  readonly pending = new Map<string, Pending>();
  private running = new Map<string, Promise<unknown>>();
  private storage?: StoragePort;
  storageAvailable = true;

  hydrate(storage: StoragePort) {
    this.storage = storage;
    try {
      const values: unknown = JSON.parse(storage.getItem(storageKey) ?? '[]');
      if (!Array.isArray(values)) throw Error('INVALID_CACHE');
      for (const value of values) {
        if (value && typeof value.identity === 'string' && value.identity.length <= 200
          && typeof value.path === 'string' && (/^\/v1\/(?:studio|feedback)\/[a-z0-9/]+$/.test(value.path) || /^\/v1\/operations\/channels\/[a-f0-9]{32}\/checks$/.test(value.path)) && !value.path.endsWith('/controls')
          && ['POST', 'PUT'].includes(value.method) && typeof value.key === 'string' && value.key.length <= 128 && value.body && typeof value.body === 'object') {
          this.pending.set(value.identity, { ...value, sensitive: false });
        }
      }
    } catch { this.storageAvailable = false; }
  }

  private persist() {
    try { this.storage?.setItem(storageKey, JSON.stringify([...this.pending.values()].filter((op) => !op.sensitive && !op.path.endsWith('/controls')))); }
    catch { this.storageAvailable = false; }
  }

  async write<T>(identity: string, path: string, body: unknown, method: 'POST' | 'PUT' = 'POST', sensitive = false): Promise<T> {
    const previous = this.pending.get(identity);
    if (previous && (previous.path !== path || previous.method !== method || JSON.stringify(previous.body) !== JSON.stringify(body))) throw Error('REQUEST_UNRESOLVED');
    const running = this.running.get(identity);
    if (running) return running as Promise<T>;
    const operation: Pending = previous ?? { identity, path, method, body, key: requestKey(), sensitive: sensitive || path.endsWith('/controls') };
    this.pending.set(identity, operation); this.persist();
    const promise = apiRequest<T>(path, { method, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.key }, body: JSON.stringify(body) })
      .then((data) => { this.pending.delete(identity); this.persist(); return data; })
      .catch((error: unknown) => {
        if (error instanceof ApiError) { this.pending.delete(identity); this.persist(); }
        throw error;
      }).finally(() => this.running.delete(identity));
    this.running.set(identity, promise);
    return promise;
  }

  retry<T>() {
    const operation = this.pending.values().next().value as Pending | undefined;
    if (!operation) return Promise.resolve(null);
    return this.write<T>(operation.identity, operation.path, operation.body, operation.method, operation.sensitive);
  }

  async importMetrics(file: File): Promise<ImportReport> {
    if (file.size > 1048576) throw new ApiError(400, 'INVALID_INPUT');
    const identity = 'loopcast.feedback.upload.' + JSON.stringify([file.name, file.size, file.lastModified]);
    let key = requestKey();
    try { key = this.storage?.getItem(identity) ?? key; this.storage?.setItem(identity, key); } catch { this.storageAvailable = false; }
    const { response, data } = await requestJSON('/v1/feedback/imports', { method: 'POST', body: file, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key, 'X-File-Name': encodeURIComponent(file.name) } });
    if (response.status >= 500 || response.ok && data === null) throw Error('REQUEST_UNRESOLVED');
    try { this.storage?.removeItem(identity); } catch { this.storageAvailable = false; }
    if (!response.ok) throw new ApiError(response.status, (data as { error?: { code?: string } })?.error?.code ?? 'INVALID_INPUT');
    return data as ImportReport;
  }

  async upload(file: File, kind: string, source: string, parent: string | null): Promise<Asset> {
    const identity = JSON.stringify([file.name, file.size, file.lastModified, kind, source, parent]);
    const keyName = 'loopcast.studio.upload.' + identity;
    let key = requestKey();
    try { key = this.storage?.getItem(keyName) ?? key; this.storage?.setItem(keyName, key); }
    catch { this.storageAvailable = false; }
    const query = new URLSearchParams({ kind }); if (parent) query.set('parent_id', parent);
    const { response, data } = await requestJSON('/v1/studio/assets/import?' + query, {
      method: 'POST', body: file,
      headers: { 'Content-Type': 'application/octet-stream', 'Idempotency-Key': key, 'X-File-Name': encodeURIComponent(file.name), 'X-Source': encodeURIComponent(source) },
    }, { totalMs: 120_000, attemptMs: 120_000, attempts: 1, backoffMs: 0 });
    if (response.status >= 500 || response.ok && data === null) throw Error('REQUEST_UNRESOLVED');
    if (!response.ok) {
      const code = (data as { error?: { code?: string } })?.error?.code;
      try { this.storage?.removeItem(keyName); } catch { /* Keep the in-memory form. */ }
      throw new ApiError(response.status, code ?? 'INVALID_MEDIA');
    }
    try { this.storage?.removeItem(keyName); } catch { /* The server owns the imported asset. */ }
    return data as Asset;
  }
}

export const platformName = (platform: string) => ({ douyin: '抖音', xiaohongshu: '小红书', bilibili: 'Bilibili', youtube: 'YouTube' }[platform] ?? platform);
export const assetURL = (id: string) => `/v1/studio/assets/${encodeURIComponent(id)}/content`;

export function draftBuffer(project: Project): { draft: Draft; baseVersion: number; available: boolean } {
  try {
    const raw = sessionStorage.getItem('loopcast.studio.draft.' + project.id);
    if (raw) {
      const value = JSON.parse(raw) as { draft: Draft; baseVersion: number };
      const draft = value.draft;
      if (!draft || typeof draft.title !== 'string' || typeof draft.script !== 'string' || typeof draft.brief !== 'string' || !Array.isArray(draft.scenes)
        || !draft.scenes.every((scene) => scene && typeof scene.id === 'string' && typeof scene.narration === 'string' && typeof scene.visual === 'string' && typeof scene.duration === 'number' && Number.isFinite(scene.duration))
        || !draft.settings || !Array.isArray(draft.settings.material_ids) || !Array.isArray(draft.settings.keywords) || !Number.isSafeInteger(value.baseVersion)) throw Error('INVALID_CACHE');
      return { ...value, available: true };
    }
  } catch { return { draft: project.draft, baseVersion: project.version, available: false }; }
  return { draft: project.draft, baseVersion: project.version, available: true };
}

export type FeedbackWorkspace = components['schemas']['FeedbackWorkspace'];
export type EditorialDecision = components['schemas']['EditorialDecision'];
export type ImportReport = components['schemas']['ImportReport'];
