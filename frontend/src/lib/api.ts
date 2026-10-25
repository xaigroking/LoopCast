import type { components, paths } from './api-schema';

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

  constructor(status: number, code: string, snapshot?: RunSnapshot) {
    super(code);
    this.status = status;
    this.code = code;
    this.snapshot = snapshot;
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
  const response = await fetch(path, { cache: 'no-store', ...options });
  const data = await response.json().catch(() => null) as ErrorEnvelope | T | null;
  if (!response.ok) {
    const error = data && typeof data === 'object' && 'error' in data ? data.error : undefined;
    throw new ApiError(response.status, error?.code ?? 'INTERNAL_ERROR', error?.snapshot as RunSnapshot | undefined);
  }
  if (data === null) throw new ApiError(response.status, 'INTERNAL_ERROR');
  return data as T;
}

export function acceptsSnapshot(previous: RunSnapshot | undefined, next: RunSnapshot): boolean {
  return /^[a-f0-9]{32}$/.test(next.run_id)
    && Number.isSafeInteger(next.state_version)
    && Number.isSafeInteger(next.last_event_id)
    && (!previous || (next.state_version >= previous.state_version && next.last_event_id >= previous.last_event_id));
}

export type ApiPaths = paths;
