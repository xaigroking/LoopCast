/** Cursor-resuming SSE with bounded reconnects and heartbeat idle detection. */
type Source = Pick<EventSource, 'addEventListener' | 'close' | 'onerror' | 'onopen'>;
type Options = {
  url: () => string;
  receive: (event: MessageEvent<string>) => boolean;
  state: (value: 'live' | 'offline' | 'requestError' | 'settled') => void;
  source?: (url: string) => Source;
  idleMs?: number;
  reconnectMs?: number;
  reconnectBudgetMs?: number;
  maxReconnects?: number;
};

export function subscribeEvents(options: Options) {
  const idleMs = options.idleMs ?? 20_000;
  const budgetMs = options.reconnectBudgetMs ?? 60_000;
  const maxReconnects = options.maxReconnects ?? 5;
  let source: Source | null = null;
  let idle: ReturnType<typeof setTimeout> | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let reconnects = 0;
  let outageStart: number | null = null;
  let reconnectDeadline: ReturnType<typeof setTimeout> | undefined;

  const closeSource = () => { source?.close(); source = null; clearTimeout(idle); };
  const stop = () => { stopped = true; closeSource(); clearTimeout(retry); clearTimeout(reconnectDeadline); };
  const exhaust = () => { stop(); options.state('requestError'); };
  const healthy = () => {
    // Opening TCP alone does not reset the budget. Require actual application
    // traffic to prevent a proxy that opens then stalls from reconnecting forever.
    clearTimeout(reconnectDeadline);
    outageStart = null;
    reconnects = 0;
    clearTimeout(idle);
    idle = setTimeout(disconnected, idleMs);
    options.state('live');
  };
  const disconnected = () => {
    if (stopped) return;
    closeSource(); // disable EventSource's otherwise unlimited automatic retry
    options.state('offline');
    if (outageStart === null) {
      outageStart = performance.now();
      reconnectDeadline = setTimeout(exhaust, budgetMs);
    }
    if (reconnects >= maxReconnects || performance.now() - outageStart >= budgetMs) { exhaust(); return; }
    const delay = Math.min(8_000, (options.reconnectMs ?? 500) * (2 ** reconnects)) * (0.5 + Math.random() * 0.5);
    reconnects++;
    retry = setTimeout(connect, delay);
  };
  const connect = () => {
    if (stopped) return;
    let current: Source;
    try { current = (options.source ?? ((url) => new EventSource(url)))(options.url()); }
    catch { disconnected(); return; }
    source = current;
    idle = setTimeout(disconnected, idleMs); // includes the connection handshake
    current.onopen = () => { if (!stopped && source === current) options.state('live'); };
    current.onerror = () => { if (!stopped && source === current) disconnected(); };
    current.addEventListener('heartbeat', () => { if (!stopped && source === current) healthy(); });
    const receive = (event: Event) => {
      if (stopped || source !== current) return;
      try {
        const complete = options.receive(event as MessageEvent<string>);
        healthy();
        if (complete) { stop(); options.state('settled'); }
      } catch {
        // Malformed/cursor-invalid data must be resynchronized explicitly.
        exhaust();
      }
    };
    current.addEventListener('snapshot', receive);
    current.addEventListener('complete', receive);
  };
  connect();
  return { close: stop };
}
