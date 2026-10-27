// SPDX-License-Identifier: Apache-2.0
export class HostError extends Error {
  constructor(code) { super(code); this.code = code; }
}

const messages = {
  en: {
    CONFIG_INVALID: 'Desktop resources are missing or invalid.',
    VERSION_MISMATCH: 'The application resources have incompatible versions.',
    PLATFORM_UNAVAILABLE: 'The required managed platform capabilities are unavailable.',
    START_FAILED: 'The local application could not start.',
    START_TIMEOUT: 'The local application did not become ready in time.',
    PROTOCOL_INVALID: 'A local component returned an invalid readiness signal.',
    COMPONENT_EXITED: 'A managed component stopped unexpectedly.',
    STOP_UNCONFIRMED: 'Shutdown could not be confirmed. Do not start another sender. Keep the existing application data for recovery.',
  },
  'zh-Hans': {
    CONFIG_INVALID: '桌面资源缺失或无效。',
    VERSION_MISMATCH: '应用资源版本不兼容。',
    PLATFORM_UNAVAILABLE: '所需的受管平台能力尚不可用。',
    START_FAILED: '本地应用启动失败。',
    START_TIMEOUT: '本地应用未能在期限内就绪。',
    PROTOCOL_INVALID: '本地组件返回了无效的就绪信号。',
    COMPONENT_EXITED: '受管组件意外停止。',
    STOP_UNCONFIRMED: '尚未确认全部资源停止。请勿启动替代发送者，并保留现有应用数据以供恢复。',
  },
};
export function safeError(error) {
  return error instanceof HostError && messages.en[error.code] ? error : new HostError('START_FAILED');
}
export function message(code, locale = 'en') {
  return (messages[locale] ?? messages.en)[code] ?? messages.en.START_FAILED;
}

// All asynchronous startup work shares one monotonic budget.
export class Budget {
  constructor(milliseconds, signal, now = () => performance.now()) {
    this.now = now; this.end = now() + milliseconds; this.signal = signal;
  }
  remaining() {
    if (this.signal?.aborted) throw new HostError('START_FAILED');
    const left = this.end - this.now();
    if (left <= 0) throw new HostError('START_TIMEOUT');
    return left;
  }
  async run(promise) {
    const owned = Promise.resolve(promise);
    owned.catch(() => {}); // Also observe work rejected after cancellation/expiry.
    const remaining = this.remaining();
    let timer, abort;
    try {
      const result = await Promise.race([owned, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new HostError('START_TIMEOUT')), remaining);
        abort = () => reject(new HostError('START_FAILED'));
        this.signal?.addEventListener('abort', abort, { once: true });
        if (this.signal?.aborted) abort();
      })]);
      this.remaining();
      return result;
    } finally {
      clearTimeout(timer); this.signal?.removeEventListener('abort', abort);
    }
  }
}
