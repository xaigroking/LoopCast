# LoopCast

A single-user task console with a separately deployed Next.js/React frontend and
FastAPI backend. The Python domain modules retain durable run history, isolated
optional video generation and the publication evidence simulator. SQLite
preserves identities, attempts, receipts and event cursors across restarts.

The default fixture mode makes no model calls. Optional generation requires a
separately provisioned MPT environment. Real platform sending and login remain
unavailable. The backend binds to loopback and has no user authentication. Keep
the reverse-proxy route behind the test environment's access controls; exact
Host/Origin checks prevent cross-site writes but do not authenticate users.

## Build and deploy

```sh
python3.12 -m pip install --require-hashes -r requirements-ci.lock
python3.12 -m pip install --require-hashes -r requirements-runtime.lock
python3.12 -m build --no-isolation
cd frontend
npm ci
npm run types:api
npm run typecheck
python ../scripts/copy-npm-licenses.py
npm run build
mkdir -p .next/standalone/public .next/standalone/.next/static
cp -a public/. .next/standalone/public/
cp -a .next/static/. .next/standalone/.next/static/
```

For a candidate that records the common Git revision and checksums, run
`scripts/build-release.sh`. It emits the backend wheel and the packed Next.js
standalone server under `dist/`, along with `release-manifest.json`. The manifest
records artifact SHA256 values and the source revision; dependency locks alone
do not claim byte-for-byte reproducible builds.

Install the Python wheel and exact runtime lock in the backend environment.
Extract the frontend archive as a separate Node service. The example systemd
units and Caddy route are in `deploy/`. Set `LOOPCAST_PUBLIC_ORIGIN` to the
exact browser-visible scheme, host and port. Caddy routes `/v1`, `/v1/*`,
`/healthz` and `/openapi.json` to FastAPI; pages and static assets go to
Next.js. Both application services bind only to loopback. Caddy forwards the
public host and scheme, which FastAPI trusts only from the configured proxy
address. Use HTTPS and the environment's access controls for a remote test
machine.

The backend prints `READY` only after storage, recovery, worker and publisher
startup completes. Its `/healthz` checks the database and worker. Check the
frontend through its own loopback URL and through Caddy. Only one backend
process can own a data directory; do not use ASGI multiple workers or reload
mode for that directory.

The `python -m loopcast serve` CLI remains as a backend-only launch command; it
no longer serves pages or static assets. Stop the old service before starting
the new backend on the same data directory. SQLite and domain tables remain in
place. For rollback, stop and drain the new backend, reinstall the previous
code and keep the current database; do not restore an older snapshot over new
writes.

## Task behavior

A fixture run freezes its input and keeps one set of logical stages: prepare, execute,
and report. Attempts append to the same run. A single worker executes fixed
operations in bounded subprocesses, with an eight-second execution deadline.

| State | Available commands |
| --- | --- |
| QUEUED | Cancel before execution |
| RUNNING | Request cancellation |
| CANCEL_REQUESTED | Wait for executor exit |
| SUCCEEDED | View history and report |
| FAILED | Explicitly retry with a new attempt |
| CANCELLED | Create a separate run if needed |

Cancellation requests do not claim completion before execution exits. An already
completed operation may win a cancellation race. On service restart, an active
attempt becomes FAILED with INTERRUPTED evidence; queued work remains queued.
Old execution tokens cannot change a new attempt. Reports are read by registered
opaque identity and checked against their stored length and SHA256.

Fixture scenarios are `success`, `fail_once` (a retry succeeds), `fail`, and
`slow`. Delay is limited to 0–3000 ms. Input is data, never a shell command.

## API

`GET /healthz` checks storage and executor readiness. `GET /v1/capabilities`
reports fixture capability and optional generation availability/verification. `/v1/runs` supports creation and
bounded listing (`limit=1..100`, then `before=next_cursor`). Details are available
at `/v1/runs/{id}`. `/cancel` and `/retry` accept `expected_version`.

Every write requires JSON, an exact `Origin` matching the local listening URL,
and an `Idempotency-Key`. Reuse a key only with the same normalized input.
Replays return the current snapshot; conflicts return 409 with a refreshable
snapshot when available. Errors contain a stable code, message and request ID.
Requests accept and return W3C `traceparent`; invalid or all-zero caller context
is replaced with a fresh trace. Request logs use structured route templates and
correlate asynchronous run attempts through a durable trace link. Trace IDs do
not authorize actions or replace idempotency keys.

```sh
curl http://127.0.0.1:8080/v1/runs \
  -H 'Origin: http://127.0.0.1:8080' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-fixture' \
  -d '{"title":"First fixture","scenario":"success"}'
```

Snapshots include a version, last event cursor and backend action policy. SSE
at `/v1/runs/{id}/events?after={cursor}` replays durable snapshots and follows
updates. Reconnects accept `Last-Event-ID`. Invalid, foreign or future cursors
return RESYNC_REQUIRED; fetch a fresh snapshot. Events are currently retained
without pruning. Event streams are capped at 16 and release on disconnect or
terminal state. The Web client rejects older versions/cursors, closes streams
when navigating, and labels disconnected views as stale. Lists poll; terminal
details settle until refresh, focus, or a local command starts a new attempt.

Report bytes are served at `/v1/artifacts/{id}/content`. The backend publishes
typed OpenAPI models and structured errors; unknown API paths return JSON 404
responses. The API service does not serve frontend pages or static assets. No
endpoint accepts arbitrary filesystem paths.

## Verification

```sh
python -m pip install --require-hashes -r requirements-ci.lock
python -m pip install --require-hashes -r requirements-runtime.lock
scripts/check-wheel.sh
scripts/check-tests.sh
python -m pip install --require-hashes -r requirements-web.lock
python -m playwright install --with-deps chromium
scripts/check-web.sh
```

The checks install the wheel and start both the production ASGI API and standalone
Next.js artifact outside the checkout. Unit and integration coverage includes
concurrent request identity, transactions, version conflicts, cancellation,
executor faults, restart recovery, lifecycle locking and event replay. Chromium
drives the combined same-origin UI at desktop and narrow widths, including
report access, retry, offline recovery, stale responses and repeated navigation.
Browser tooling is locked for Linux x86_64 and CPython 3.12. Node/npm, frontend
runtime packages and Python runtime dependencies are pinned. Fonts and language
bundles are included in the frontend archive, with no runtime CDN requests.

## Optional isolated video generation

Generation uses a separate Linux environment. Install MoneyPrinterTurbo from
revision `82bb4a7502704559575f4e3d002b9dcb31d4ed6a` into its own virtual
environment, retaining the built wheel and its SHA256. The wheel digest identifies
the candidate artifact; the source revision is an operator provenance declaration.
For example, with Python 3.12 and Git available, provision the candidate outside
this checkout (provider-specific dependencies still follow the upstream project):

```sh
python3.12 -m venv /opt/mpt
/opt/mpt/bin/python -m pip wheel --no-deps --wheel-dir /opt/components \
  'git+https://github.com/harry0703/MoneyPrinterTurbo.git@82bb4a7502704559575f4e3d002b9dcb31d4ed6a'
/opt/mpt/bin/python -m pip install /opt/components/mpt-*.whl
sha256sum /opt/components/mpt-*.whl
```

This provisioning recipe is not a locked transitive dependency environment or a
claim of a successful live run; retain and independently validate its dependency
inventory before use. The control plane never installs upstream packages for you.

LoopCast checks installed wheel files before calling the public CLI. This is
process and dependency isolation, not an OS security sandbox. Provision only
trusted code. Generation providers may require network access and credentials.

Use a trusted local JSON file with these fields:

```json
{
  "python": "/opt/mpt/bin/python",
  "wheel": "/opt/components/mpt-1.3.9-py3-none-any.whl",
  "ffprobe": "/opt/media-tools/ffprobe",
  "ffprobe_sha256": "<SHA256 of the provisioned probe>",
  "config_ref": "/private/generation-provider.toml",
  "timeout": 300,
  "manifest": {
    "source": "https://github.com/harry0703/MoneyPrinterTurbo",
    "revision": "82bb4a7502704559575f4e3d002b9dcb31d4ed6a",
    "artifact_sha256": "<SHA256 of the provisioned wheel>",
    "contract_major": 1,
    "verification": "unverified",
    "capabilities": {
      "generate": true, "publish": false, "query": false,
      "schedule": false, "ai_disclosure": false
    }
  }
}
```

Replace digest placeholders with actual SHA256 values. Start with
`python -m loopcast serve --generator-config /private/generator.json` and select
MPT in the creation form, or POST `{"mode":"mpt","title":"Your subject"}` to
`/v1/runs` using the normal same-origin and idempotency headers. Unconfigured
services reject this mode. Restarting without the matching component cannot
silently execute queued generation as a fixture.

The provider TOML accepts only an `[app]` section with generation provider
settings: `llm_provider`, `openai_api_key`, `openai_base_url`, `openai_model_name`,
`azure_api_key`, `azure_model_name`, `azure_base_url`, `pexels_api_keys`, and
`pixabay_api_keys`. Both `upload_post_enabled` and `upload_post_auto_upload` are
forced false; enabling either or adding undeclared settings is rejected. Provider
configuration is copied into a private per-attempt runtime directory and removed
on completion. Only the reference is configured locally; secrets never enter the
run snapshot. Inherited environment variables are not forwarded to the runner.
A hard control-plane kill can leave an unfinished work directory containing the
configuration: keep the entire data directory private and remove recovered
interrupted work directories before reuse. Do not share a data directory.

The subprocess group has a deadline, cancellation and output bounds. A matching
task JSON and one nonempty MP4 with a valid video stream, duration and dimensions
are required. Media is copied into managed immutable storage and linked to its
attempt and logical content. Artifacts are served through opaque identities and
checked against their stored SHA256. Database upgrades are transactional; unknown
schema versions are rejected. Back up the complete private directory while the
service is stopped before upgrading. Downgrading a migrated database is unsupported.

The automated generator checks use an independently installed CLI contract double
and a tiny synthesized orange video. They prove adapter behavior and media
validation, **not a successful real MPT/provider execution**. Configured generation
is labeled live unverified. No real platform is declared supported.

## Publication evidence simulator

Completed runs can add named local simulator targets. Each target has a permanent
identity for platform, stable account and logical content, plus append-only
attempts and receipts. Changing titles, request keys or media bytes does not reset
that identity. Run retry is disabled once publication intents exist. There is no
real uploader, account login or external sending switch.

The same-origin, idempotent endpoints are:

- `POST /v1/runs/{run}/publications`: `platform` (always `simulator`), `account`,
  and `expected_version`.
- `POST /v1/runs/{run}/publications/{intent}/dispatch`: `expected_version` and
  optional simulator `scenario`: `success`, `submitted`, `lost_response`,
  `not_sent`, or `slow`.
- `POST /v1/runs/{run}/publications/{intent}/verify`: `expected_version` only.

The backend owns action policy and account exclusion. Before-send interruption
can become NOT_SENT; interruption after the send boundary becomes UNKNOWN and
blocks another send. Expired leases never authorize a replacement sender.
`submitted`, exit code zero and missing work identity do not mean PUBLISHED.
The query action reads a durable simulator receipt and requires observed sender
exit before releasing the account lock. Missing evidence or a control-plane
crash with an unproven sender exit stays UNKNOWN, including across restarts;
there is no force-confirm button or generic retry escape hatch. A successful
target remains settled while another target is unresolved. All outcomes remain
explicitly simulated.

Before any future real sending integration, implement and verify restore barriers
so restoring an older backup cannot authorize another send. This version does not
provide that operational guarantee and keeps real sending unavailable.

## Media checks

For Linux x86_64, optional test tools are downloaded from a public static FFmpeg
7.0.2 distribution and verified against a pinned archive SHA256. The archive
contains its license information; it is kept outside the application wheel.

```sh
export LOOPCAST_MEDIA_TOOLS=/tmp/loopcast-media-tools
scripts/install-media-tools.sh
scripts/check-tests.sh
scripts/check-web.sh
```

The installed backend integration suite covers fixture and generation contracts,
SQLite recovery, executor faults, SSE replay/disconnects and simulated publication
evidence. Chromium launches the installed wheel and production Next standalone
server behind a same-origin streaming proxy. It checks deep links, fixture
creation/report access, API routing, locale packs, RTL/theme and narrow layouts.
CI requires package, tests and browser to succeed; missing or skipped jobs fail
the merge gate. Public CI needs no provider or platform credentials.

## Workspace languages and appearance

Open `/settings` from either desktop or narrow-screen navigation. The workspace
ships English, Simplified Chinese, Traditional Chinese, Japanese, Korean, French,
German, Spanish, Brazilian Portuguese, Italian, Dutch, Polish, Russian, Turkish,
Arabic, Hebrew, Hindi, Indonesian, Vietnamese and Thai. The selector is searchable
and shows native names alongside names in the current display language.

Offline i18next and its browser language detector select the first supported
entry in the browser's ordered language list. An explicit selection takes
priority; "Follow browser" restores automatic detection and responds to browser
language changes. English regions map to English. Bare Chinese, CN, SG and Hans
map to Simplified Chinese; TW, HK, MO and Hant map to the separate Traditional
Chinese pack. Portuguese variants use the shipped Brazilian Portuguese pack.
Unsupported languages fall back to English. No IP lookup or online translation
service is used. Display language is separate from generated content language.

Arabic and Hebrew switch the document direction. Layout uses logical properties;
raw titles, identifiers, hashes and source evidence retain their original content
and direction isolation. Intl/CLDR handle dates, numbers and task-count plurals.
Unknown server errors show a safe localized message. Missing selected resources
show an explicit warning and English fallback; release checks reject incomplete
bundles and mismatched interpolation parameters before packaging.

Appearance supports system, light and dark. System changes apply while automatic
mode is selected; a manual selection takes priority. A small pre-stylesheet
script restores the initial theme. Versioned local storage contains display
preferences only. Blocked or corrupt storage does not prevent session use.
Cross-tab preference changes use the most recently received complete preference
record. Language/theme changes do not create tasks or attempts, reset request
identities, or clear in-progress task/account/scenario drafts. Work records and
permanent publication identities remain in SQLite.

One ribbon SVG supplies the sidebar, header, settings and favicon. Color and
monochrome variants at 16/24/32/128 pixels retain identical paths. The application
has no PWA or installer icon surfaces. npm dependencies are pinned by
`frontend/package-lock.json`; generated standalone assets include each locked
package's declared license and available notice files. Noto fonts have pinned
SHA256 inventories in `web-fonts.lock.json`. There are no remote font or CDN
requests at runtime. Resource validation includes actual OpenType glyph coverage
for the shipped text. UI preview cards use document icons for fixture reports,
not fabricated video thumbnails.

`python scripts/check-ui-assets.py` checks all 20 locale contracts, dependency
versions, font hashes, OFL notices and glyph coverage. `scripts/check-web.sh`
starts both production artifacts and routes browser traffic through a local
streaming same-origin proxy. Browsers without secure randomness show a recoverable
error without sending an unkeyed write.
