import type { components, paths } from './api-schema';
import { requestJSON, TransportError } from './http';

export type RunSnapshot = components['schemas']['RunSnapshot'];
export type RunPage = components['schemas']['RunPage'];
export type ErrorEnvelope = components['schemas']['ErrorEnvelope'];
export type RunCreate = components['schemas']['FixtureCreate'] | components['schemas']['MPTCreate'];
export type RunAction = 'cancel' | 'retry';
export type PublicationAction = 'dispatch' | 'verify';

export class ApiError extends Error {
  status: number;
  code: string;
  snapshot?: RunSnapshot;
  requestId?: string;

  constructor(status: number, code: string, snapshot?: RunSnapshot, requestId?: string) {
    super(code);
    this.status = status;
    this.code = code;
    this.snapshot = snapshot;
    this.requestId = requestId;
  }
}

export function requestKey(source: Crypto = globalThis.crypto): string {
  if (typeof source?.randomUUID === 'function') return source.randomUUID();
  if (typeof source?.getRandomValues !== 'function') throw new Error('SECURE_RANDOM_UNAVAILABLE');
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

export async function apiRequest<T>(path: keyof paths | string, options: RequestInit = {}): Promise<T> {
  const { response, data: raw } = await requestJSON(path, options);
  const data = raw as ErrorEnvelope | T | null;
  const write = !['GET', 'HEAD'].includes((options.method ?? 'GET').toUpperCase());
  // A 5xx or unreadable success body cannot prove that a write did not commit.
  // Keep the caller's pending identity for an explicit retry with the same key.
  if ((write && response.status >= 500) || (response.ok && data === null)) throw new TransportError('NETWORK_ERROR');
  if (!response.ok) {
    const error = data && typeof data === 'object' && 'error' in data ? data.error : undefined;
    throw new ApiError(response.status, error?.code ?? 'INTERNAL_ERROR', error?.snapshot as RunSnapshot | undefined,
      error?.request_id ?? response.headers.get('X-Request-ID') ?? undefined);
  }
  return data as T;
}

export function acceptsSnapshot(previous: RunSnapshot | undefined, next: RunSnapshot): boolean {
  return /^[a-f0-9]{32}$/.test(next.run_id)
    && Number.isSafeInteger(next.state_version)
    && Number.isSafeInteger(next.last_event_id)
    && (!previous || (next.state_version >= previous.state_version && next.last_event_id >= previous.last_event_id));
}

export type ApiPaths = paths;

export async function assetText(id: string): Promise<string> {
  const { response, data } = await requestJSON(`/v1/studio/assets/${encodeURIComponent(id)}/content`, {}, undefined, 'text');
  if (!response.ok || typeof data !== 'string' || data.length > 20000) throw new ApiError(response.status, 'INVALID_MEDIA');
  return data;
}
