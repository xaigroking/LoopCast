# Linux development desktop host

This directory contains a thin Electron host for the existing Python owner and
production Next standalone UI. It includes a lifecycle coordinator, a fixed
streaming same-origin entry, an isolated window policy, and subprocess contract
tests. Linux x86_64 now has an installed Python subreaper bridge, real managed backend
protocol and opt-in desktop session. Missing capabilities cause startup to fail.
Windows/macOS production ports and actual Electron window acceptance remain
separate, unverified work.

Electron is pinned to **45.0.0**. Next runs under a separate, absolute **Node
24.22.1** executable, not Electron's embedded Node. The npm lock records source
package integrity; it does not verify Electron, Python or Node target binaries.
Production provisioning must record the platform, source revisions, runtime
origins and binary digests. There is no runtime downloader or automatic updater.

## Source verification

With Node 24.22.1 and npm 11.19.1:

```sh
cd desktop
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run typecheck
npm test
```

These commands download npm source/type packages, run syntax checks, check the
window policy against Electron's declarations, and execute contract tests. They
do not download Electron binaries, compile a desktop app, or build an installer.
CI keeps the existing package, integration and browser checks and adds this
source-only job to the merge gate.

The process tests use independent children with no descendants or business
store. Their test-only port must never be shipped as a ProcessTree implementation.
The Next wrapper test uses a generated-server substitute. Tests exercise binary
upload, Range responses, live SSE and disconnect cancellation through the actual
fixed gateway, with substitute upstreams. Window tests invoke policy callbacks;
they do not establish Chromium sandbox behavior or actual window operation.

## Trusted resources and development entry

Once independently verified runtimes and the platform port are available, a
developer can start the host with `npm start -- --desktop-config /absolute/desktop.json`.
Electron must have been provisioned separately; the verification install above
intentionally does not install its binary. A packaged host instead uses the
fixed `resources/loopcast/desktop.json` path and requires `mode: "release"`.
No renderer API accepts executables, argv, resource paths or private settings.

The trusted manifest has these required fields:

| Field | Contract |
| --- | --- |
| `format`, `mode` | `loopcast.desktop.v1`; `development` or `release` |
| `hostRevision`, `apiRevision`, `webRevision` | Actual 40 character source revisions, separately recorded |
| `nodeVersion` | `24.22.1` |
| `python`, `node`, `treeModule` | Each contains an absolute `path` and a 64 character `sha256` |
| `nextRoot` | Absolute standalone root containing `server.js`, `.next/BUILD_ID`, `.next/static` and `public` |
| `dataDir` | Existing trusted business data directory, validated by the platform port |
| `sessionDir` | Existing private Electron session directory, separate from business and installation resources |
| `gatewayPort` | Stable, exclusively bound loopback entry port from 1 to 65535 |
| `configs` | Optional absolute `generator`, `media`, `social`, `schedule`, `inspection` JSON references |
| `startTimeoutMs`, `stopTimeoutMs` | Positive bounded monotonic lifecycle budgets (100–120000 and 100–60000) |
| `locale` | `en` or `zh-Hans` for host messages; existing UI language selection remains independent |

Resource hashes and the frontend BUILD_ID are checked before launch. A trusted
manifest and immutable installed resource tree are provisioning requirements;
hashing a file is not a substitute for securing its parent directory. No
application, data migration or runtime download is performed by this loader.

Keep `gatewayPort`, `sessionDir` and the persistent browser partition stable
across restarts/upgrades. Browser storage and the owner cookie are scoped to
the origin: choosing a new entry port would lose access to existing pending
write identities. A port collision fails without contacting the existing
listener or choosing a new origin. API and Next each bind port zero directly;
their actual ports arrive over this launch's private child pipes. No free-port
probe is used. Clearing HTTP cache must not clear localStorage, IndexedDB,
cookies or Python data. Session cache removal and business data removal are
different operations; this candidate provides no data deletion command.

## Required platform and backend contracts

The trusted `treeModule` exports `treePort` with the
[`loopcast.process-tree.v1` interface](src/platform-contract.d.ts). The Linux module
`src/linux-tree.mjs` implements this boundary through the installed Python
`loopcast.process_tree` subreaper guardian. It is not a Windows/macOS port.
It validates private directories and any unresolved recovery barriers, returns
minimal per-role environments and synchronously transfers ownership of launched
children. It must contain the complete process tree before user code executes
and supply private pipes, monotonic deadlines and complete-tree shutdown
evidence even if the leader exits first. Parent death and unconfirmed cleanup
must preserve the backend's recovery barrier across host restarts. There is no
parent-only kill fallback.

The exact Python executable is probed with
`-I -m loopcast platform-info`. Required JSON fields are:

```json
{
  "format": "loopcast.platform.v1",
  "source_revision": "<actual API source revision>",
  "capabilities": {
    "file_access": "implemented",
    "owner_lock": "implemented",
    "sqlite_access": "implemented",
    "process_tree": "implemented",
    "managed_stop": "implemented",
    "desktop_session": "implemented"
  }
}
```

The installed Linux backend implements this contract. Source identity is embedded
when building both sdist and wheel; dirty or unidentified sources report
`unavailable` and cannot pass the desktop combination check.
`implemented` alone never means native-platform verification has passed.

The API is launched with the existing controlled arguments plus
`--managed-stdin --desktop-session-stdin`. Its stdin first receives one bounded
JSON line with format `loopcast.desktop-session.v1` and a fresh 64 hex character
`token`; later input permits only `STOP` or EOF. The token is not in argv,
environment, URL, browser storage or logs. Desktop mode requires this bearer
in `X-LoopCast-Desktop-Session` for all API traffic, except the scoped readiness
probe below. Default service-mode authentication behavior stays unchanged.

Before exposing the UI, the host validates `/healthz` using a random nonce in
`X-LoopCast-Desktop-Probe` and a request MAC in `X-LoopCast-Desktop-Proof`. The
backend validates that MAC and returns `{"ready":true}` plus a response MAC in
the same proof header. Both MACs use HMAC-SHA256 with the token **string** as
key and UTF-8 `JSON.stringify` of this array as the message:

```text
["loopcast.desktop-health.v1", direction, nonce, apiOrigin, publicOrigin, apiRevision]
```

`direction` is `request` or `response`; origins include their exact ports. The
backend derives its bound API origin and configured public origin itself and
uses its actual source revision. Proof headers are lowercase hex and compared
in constant time. This capability is scoped to health probes; it cannot authorize
business routes. A syntactically valid READY pointing to a stranger cannot
receive the API bearer, reflect a request MAC into a response, or pass this
check by returning `ready:true`. The authenticated private READY and source
probe do not attach an existing CLI/system service.

API stdout permits exactly one `READY http://127.0.0.1:<bound-port>` line and one
`STOPPED` line after resources and the owner lock close. Cancellation can produce
STOPPED before READY. Lines are bounded and strict ASCII. Logs go to stderr and
are drained without including raw content in host messages. Next's wrapper
forces the actual HTTP bind to loopback/port zero and sends readiness through
private fd 3, leaving framework output separate. It runs the generated
standalone entry; its HTTP listen hook needs verification with each real Next
candidate before distribution.

## Session, routing and shutdown

The Electron main process injects a separate per-start entry capability only
for this window's main frame and exact entry origin. No preload or IPC bridge is
installed. Frames, new windows, external navigation/redirects, downloads and
permissions are denied. The window enables context isolation, sandbox and web
security and disables Node integration. The existing nonce CSP, offline fonts
and dynamic routes remain in Next; the window adds `frame-src 'none'`.

The gateway applies the existing deployment route table: exact `/v1` and
`/v1/*`, `/healthz`, `/openapi.json` go to API; remaining routes go to Next. It
requires its capability and exact Host/Origin, rejects ambiguous/absolute
paths, strips hop and forged forwarding headers, and attaches the backend
capability only to API traffic. It streams request/response bodies with
backpressure and cancels upstream streams on disconnect. It does not provide
renderer-selected proxy destinations, CONNECT or WebSocket tunnels.

`STOPPED → STARTING → READY → STOPPING` coordinates all resources. Duplicate
starts/stops share operations. Closing the window also quits on macOS; no tray
or background restart is provided. Shutdown destroys the window, stops entry
admission/streams, sends private STOP to API, then closes other owned trees and
the entry. Safe STOPPED requires API STOPPED, exit zero and confirmed complete
trees. A bounded timeout or missing evidence leaves STOPPING, reports
`STOP_UNCONFIRMED` and prevents another launch in this host. Restart protection
after host death depends on the required platform/backend recovery barrier.
Crash handling never replays a generation or publication operation.

The production integration test executes the actual installed backend, Next,
coordinator and Linux port; the browser restart test uses real Chromium with a
persistent private profile. Chromium driver capability headers do not validate
Electron frame/initiator behavior. The native CI also runs the actual verified Electron executable with default
renderer isolation on a sandbox-capable Xvfb display. Windows/macOS ports,
target runtime distribution, native builds, installers, signing, upgrades and
clean-machine acceptance remain separate, unverified work. Existing Python ownership, permanent
publication ledger, UNKNOWN, recovery guard/epoch and plan facts remain owned by
the backend; this host never migrates, merges, clears or restores them.

Electron API contracts were checked against its pinned npm declarations and
[security guidance](https://www.electronjs.org/docs/latest/tutorial/security),
[webContents](https://www.electronjs.org/docs/latest/api/web-contents),
[webRequest](https://www.electronjs.org/docs/latest/api/web-request), and the
[Next standalone contract](https://nextjs.org/docs/app/api-reference/config/next-config-js/output).
The npm Electron package is MIT licensed. Future binary distributions must
include Electron/Chromium and all other runtime notices in addition to this
repository's existing application, dependency and font notices.


## Read-only planning and production integration

`node desktop/scripts/prepare.mjs --python /absolute/venv/bin/python --node
/absolute/node --next /absolute/standalone --data /absolute/private-data
--session /absolute/private-session --port 18786 --revision <source-sha>` emits
a development manifest to its caller. All paths are trusted operator input. It
checks resource digests, installed source identity, capabilities and existing
private directories without creating a store, migrating data or downloading
runtimes. Save its output in a private file. A venv interpreter's symlink is
hashed through its target but its original absolute launch path is preserved.

`bash scripts/check-desktop.sh` requires the normal same-source wheel/standalone
release build and existing locked media/browser tools. It exercises actual
production processes; it does not substitute fixture-port for the platform.
`run-local.mjs` is a windowless coordinator entry for that harness, not a public
HTTP credential issuer. Its bootstrap capability goes only to a caller-owned
private descriptor (default fd3); stdout contains only READY.

The Linux guardian enables PR_SET_CHILD_SUBREAPER before business code and
retains private control and witness pipes. On leader exit or host pipe EOF it
terminates adopted descendants with pidfds and reaps until the kernel returns
ECHILD. Session changes or double forks do not make descendants unowned. An
exit status without a valid private tree witness is unconfirmed. This is
process ownership, not an OS sandbox against arbitrary privileged code.

Before desktop admission, the Python owner creates and syncs a private
`desktop-runtime.guard.v1` marker with format `loopcast.desktop-guard.v1` and a
random run identity. This runtime safety marker does not change schema9,
publication facts or the backup format. Every new service/offline owner refuses
an existing unresolved marker. Only this launch's normal API STOPPED/exit0 and
complete-tree evidence for every component allow the coordinator to finalize
its matching marker under the original service lock. Host/guardian death or
missing evidence leaves it in place. There is no automatic expiration or
recovery bypass. Older application versions do not understand this marker:
never downgrade an unresolved directory to an older sender. Manual recovery
needs a separately reviewed procedure, preserving the original data.

Non-sensitive pending browser writes use independent localStorage records per
idempotency key, preserving concurrent windows and surviving a destroyed
window/host restart. Existing sessionStorage records migrate without replay.
Sensitive authorization/control payloads are excluded. A new window restores
the frozen creation payload and key for explicit retry; it never sends it on
reload automatically. HTTP cache clearing does not remove these identities.

## Offline desktop diagnostics

Use a trusted installed interpreter, independently of the manifest's tools:

```
python -I -m loopcast desktop-diagnostics --manifest /trusted/config/desktop.json
python -I -m loopcast desktop-diagnostics --data-dir /existing/private-data --format text
```

This command never executes the manifest's Python, Node or tree module, starts
services, contacts a provider, or opens a Store. Missing directories, locks and
DBs stay missing. It reports existing lock contention, the desktop cleanup
marker, the database restore publication guard and UNKNOWN publication counts
as separate observations. A free lock or absent PID proves nothing about full
process-tree cleanup. The report cannot authorize recovery or sending.

Reports use `loopcast.desktop-diagnostics.v1` with fixed fields, reason codes,
sources, observation times and explicit verified/unverified/unavailable states.
`verified` describes one observation, including observed faults; it never means
that startup or sending is safe. Platform implementation metadata does not
prove default sandbox or real provider compatibility. Multiple observations
are not a global atomic snapshot. Stable bounded private DB/WAL copies feed a
query-only SQLite transaction; source files and sidecars are not changed.
Continuously changing or unsafe input produces unavailable facts.

The default total budget is 2000 ms and 256 MiB of reads. `--timeout-ms` accepts
1–10000 and `--max-bytes` accepts up to 512 MiB. Cancellation and exhausted
budgets leave unfinished checks unavailable. Exit zero means a report was
constructed, including any reported faults; it is not permission to start.
Exit one means incomplete collection or failed export; two means invalid CLI
arguments. Reports contain no paths, credentials, business content, raw errors
or cleanup identities. JSON is the default; `--format text` adds brief guidance.

`--output /private/reports/new-report.json` requires an existing private report
directory separate from data, session, installation, manifest and credential
configuration directories and their ancestors. Only a new file is published,
with mode 0600 and atomic no-replace semantics. Existing targets, links,
replacement parents and protected destinations are refused. A failed export
does not change application state or leave a completed report at the target.

For resource mismatches, check trusted resources built from the same source,
then run diagnostics again. For contention, close the original application
normally. For unavailable platform/runtime evidence, obtain the relevant OS
and runtime evidence. For an unresolved cleanup/publication barrier, retain
the existing data and execution evidence for operator investigation. No command
or button clears a barrier, repairs data, unlocks an owner, replays UNKNOWN work
or resumes sending. Do not downgrade an unresolved directory to an older sender.

桌面离线诊断只读现有状态，不启动第二 owner。报告中的 `verified` 只表示单项观察有证据，
不表示可以安全恢复。桌面清理屏障、数据库恢复发布屏障和锁占用分别列出；无法读取就报告未知。
资源错配应核对同源码可信资源；占用应正常关闭原实例；未确认清理和发布屏障应保留原目录、
业务事实与进程证据，交运行环境维护者核查。本命令不提供清屏障、强制解锁或恢复发送功能。


Startup failures use the same bounded native error mechanism, with explicit
English/Simplified Chinese causes and static offline instructions. An early
failure keeps the no-fallback-profile exit path. No diagnostic subprocess is
added while showing an error, and diagnostic failure cannot replace the
original startup cause or extend its budget. The production directory preflight
returns one bounded canonical allowlisted result; malformed output and missing
tree evidence never become a successful observation. No clipboard or renderer
bridge is introduced.

The required Windows diagnostic smoke check verifies an actual unsupported OS:
it emits `PLATFORM_UNSUPPORTED` without touching the requested directories.
This verifies truthful reporting, not a Windows file/process-tree port or
Electron compatibility. Linux provides the offline file/SQLite observations.
