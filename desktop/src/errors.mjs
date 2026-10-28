// SPDX-License-Identifier: Apache-2.0
export class HostError extends Error {
  constructor(code) { super(code); this.code = code; }
}

const messages = {
  en: {
    MANIFEST_MISSING: 'The desktop manifest is missing.',
    MANIFEST_INVALID: 'The desktop manifest has an invalid format.',
    MANIFEST_CHANGED: 'The desktop manifest changed during startup.',
    PATH_UNSAFE: 'A desktop resource path is unsafe.',
    RESOURCE_MISSING: 'A required desktop resource is missing.',
    RESOURCE_DIGEST_MISMATCH: 'A desktop resource does not match its trusted digest.',
    DIRECTORY_MISSING: 'A required private directory is missing.',
    DIRECTORY_UNSAFE: 'A desktop directory does not have the required private permissions.',
    FILE_UNSAFE: 'A protected desktop file is unsafe.',
    PERMISSION_DENIED: 'A required desktop observation was denied.',
    MAINTENANCE_UNCONFIRMED: 'An unresolved offline maintenance operation is present. Preserve the existing evidence.',
    RESTORE_SOURCE_REQUIRED: 'A trusted completed restore receipt is required. Keep the independent backup evidence and upload barrier.',
    HANDOFF_UNCONFIRMED: 'An application-wide handoff is unresolved. Preserve the original profiles and execution evidence.',
    OWNER_HELD: 'The existing application data is owned by another instance.',
    ENTRY_PORT_BUSY: 'The configured desktop entry port is occupied.',
    DESKTOP_CLEANUP_UNCONFIRMED: 'An unresolved desktop cleanup marker is present.',
    GUARD_UNSAFE: 'The desktop cleanup marker cannot be safely read.',
    DIAGNOSTICS_UNAVAILABLE: 'The startup diagnostic evidence is unavailable.',
    PLATFORM_UNSUPPORTED: 'This desktop platform is not supported.',
    CAPABILITY_UNAVAILABLE: 'A required managed platform capability is missing.',
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
    MANIFEST_MISSING: '缺少桌面配置清单。',
    MANIFEST_INVALID: '桌面配置清单格式无效。',
    MANIFEST_CHANGED: '桌面配置清单在启动期间发生了变化。',
    PATH_UNSAFE: '桌面资源路径不安全。',
    RESOURCE_MISSING: '缺少必要的桌面资源。',
    RESOURCE_DIGEST_MISMATCH: '桌面资源与可信摘要不符。',
    DIRECTORY_MISSING: '缺少必要的私有目录。',
    DIRECTORY_UNSAFE: '桌面目录未满足私有权限要求。',
    FILE_UNSAFE: '受保护的桌面文件不安全。',
    PERMISSION_DENIED: '必要的桌面取证读取被拒绝。',
    MAINTENANCE_UNCONFIRMED: '检测到尚未确认的离线维护操作，请保留现有证据。',
    RESTORE_SOURCE_REQUIRED: '缺少可信的已完成恢复回执。请保留独立备份证据和上传屏障。',
    HANDOFF_UNCONFIRMED: '存在应用级未决交接，请保留原工作区、浏览器配置与执行证据。',
    OWNER_HELD: '现有应用数据正被另一个实例占用。',
    ENTRY_PORT_BUSY: '配置的桌面入口端口已被占用。',
    DESKTOP_CLEANUP_UNCONFIRMED: '检测到尚未确认清理的桌面屏障。',
    GUARD_UNSAFE: '无法安全读取桌面清理屏障。',
    DIAGNOSTICS_UNAVAILABLE: '启动诊断证据不可得。',
    PLATFORM_UNSUPPORTED: '尚未支持此桌面运行平台。',
    CAPABILITY_UNAVAILABLE: '缺少必要的受管平台能力。',
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
  return error instanceof HostError && Object.hasOwn(messages.en, error.code) ? error : new HostError('START_FAILED');
}
export function message(code, locale = 'en') {
  return (messages[locale] ?? messages.en)[code] ?? messages.en.START_FAILED;
}

// Static guidance only: no offline collector or extra subprocess during failure.
export function failureDetails(error, locale = 'en', secondary) {
  const code = safeError(error).code, extra = secondary ? safeError(secondary).code : undefined;
  const keep = ['DESKTOP_CLEANUP_UNCONFIRMED', 'MAINTENANCE_UNCONFIRMED', 'HANDOFF_UNCONFIRMED', 'GUARD_UNSAFE', 'STOP_UNCONFIRMED'].includes(code) || extra === 'STOP_UNCONFIRMED';
  const occupied = ['OWNER_HELD', 'ENTRY_PORT_BUSY'].includes(code);
  const unsupported = ['PLATFORM_UNSUPPORTED', 'PLATFORM_UNAVAILABLE', 'CAPABILITY_UNAVAILABLE'].includes(code);
  if (locale === 'zh-Hans') return `安全码：${code}${extra && extra !== code ? `；附加证据：${extra}` : ''}\n证据范围：仅启动观察；未收集完整离线报告。\n` +
    (keep ? '保留原目录、业务事实和进程证据，交运行环境维护者取证。锁空闲不证明全部资源已停止。' :
      occupied ? '请正常关闭原实例，核对固定入口配置。' : unsupported ? '请取得目标系统及运行环境证据；本报告不证明平台兼容。' : '请核对同源码的可信资源和私有目录后重新检查。') +
    '\n只读诊断命令（使用受信解释器和已有清单）：\npython -I -m loopcast desktop-diagnostics --manifest <trusted-manifest>\n本报告不能确认安全恢复或授权发送；没有清屏障、自动修复或重投入口。';
  return `Safe code: ${code}${extra && extra !== code ? `; additional evidence: ${extra}` : ''}\nEvidence: startup observation only; full offline report not collected.\n` +
    (keep ? 'Keep the existing data and process evidence for operator investigation. A free lock does not prove complete cleanup.' :
      occupied ? 'Close the original instance normally and check the fixed entry configuration.' : unsupported ? 'Obtain target OS and runtime evidence; this report does not prove compatibility.' : 'Check trusted resources from the same source and private directories, then inspect again.') +
    '\nRead-only diagnostics (trusted interpreter and existing manifest):\npython -I -m loopcast desktop-diagnostics --manifest <trusted-manifest>\nThis report cannot confirm safe recovery or authorize sending. No barrier-clearing, automatic repair or replay operation is provided.';
}
export function failureText(error, locale) {
  return message(safeError(error).code, locale) + '\n\n' + failureDetails(error, locale);
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
