/** Finite budgets for JSON requests. Only reads are automatically replayed. */
export type RequestPolicy = { totalMs: number; attemptMs: number; attempts: number; backoffMs: number };
const defaultPolicy: RequestPolicy = { totalMs: 10_000, attemptMs: 4_000, attempts: 3, backoffMs: 200 };
const transient = new Set([408, 429, 502, 503, 504]);

export class TransportError extends Error {
  constructor(public code: 'REQUEST_TIMEOUT' | 'NETWORK_ERROR') { super(code); }
}

function abortError() { return new DOMException('Request cancelled', 'AbortError'); }

function wait(ms: number, signal?: AbortSignal | null) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) { reject(abortError()); return; }
    const cancel = () => { clearTimeout(timer); reject(abortError()); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve(); }, ms);
    signal?.addEventListener('abort', cancel, { once: true });
  });
}

export async function requestJSON(path: string, options: RequestInit = {}, policy: RequestPolicy = defaultPolicy) {
  const read = ['GET', 'HEAD'].includes((options.method ?? 'GET').toUpperCase());
  const attempts = read ? Math.min(3, Math.max(1, policy.attempts)) : 1;
  const deadline = performance.now() + policy.totalMs;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (options.signal?.aborted) throw abortError();
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new TransportError('REQUEST_TIMEOUT');
    const controller = new AbortController();
    const cancel = () => controller.abort();
    options.signal?.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(cancel, Math.min(read ? policy.attemptMs : policy.totalMs, remaining));
    try {
      const response = await fetch(path, { cache: 'no-store', ...options, signal: controller.signal });
      if (read && transient.has(response.status) && attempt + 1 < attempts) {
        await response.body?.cancel();
      } else {
        // The same deadline covers headers and reading the response body.
        const data: unknown = await response.json().catch((error: unknown) => {
          if (controller.signal.aborted) throw error;
          return null;
        });
        return { response, data };
      }
    } catch {
      if (options.signal?.aborted) throw abortError();
      if (!read || attempt + 1 === attempts) throw new TransportError(controller.signal.aborted ? 'REQUEST_TIMEOUT' : 'NETWORK_ERROR');
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', cancel);
    }
    const delay = policy.backoffMs * (2 ** attempt) * (0.5 + Math.random() * 0.5);
    if (performance.now() + delay >= deadline) throw new TransportError('REQUEST_TIMEOUT');
    await wait(delay, options.signal);
  }
  throw new TransportError('NETWORK_ERROR');
}
