# LoopCast

A local, single-user task console with durable history, isolated optional video
generation, and a publication evidence simulator. Create fixture runs, follow
execution stages, cancel or retry, and open verified reports and media. SQLite
preserves identities, attempts, receipts and event cursors across restarts.

The default fixture mode makes no model calls. Optional generation requires a
separately provisioned MPT environment. Real platform sending and login remain
unavailable. This prototype binds to loopback and has no user authentication;
it is intended for one local user on Linux with Python 3.12 or newer.

## Build and start

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-ci.lock
python -m build --no-isolation
python -m venv /tmp/loopcast-install
python -m pip --python /tmp/loopcast-install/bin/python install --no-deps dist/*.whl
/tmp/loopcast-install/bin/python -I -m loopcast serve \
  --host 127.0.0.1 --port 8080 --data-dir /tmp/loopcast-data
```

Open `http://127.0.0.1:8080`. `--port 0` selects an available port; stdout prints
`READY` and the listening URL after the store and executor initialize. Stop with
Ctrl+C or SIGTERM. Only one service can own a data directory at a time.
Runtime code uses the Python standard library; Web assets ship inside the wheel.

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

Report bytes are served at `/v1/artifacts/{id}/content`. API errors do not fall
through to the Web router. No endpoint accepts arbitrary filesystem paths.

## Verification

```sh
python -m pip install --require-hashes -r requirements-ci.lock
scripts/check-wheel.sh
scripts/check-tests.sh
python -m pip install --require-hashes -r requirements-web.lock
python -m playwright install --with-deps chromium
scripts/check-web.sh
```

The checks install the wheel into independent environments and start services
outside the checkout. Unit and integration coverage includes concurrent request
identity, transactions, version conflicts, cancellation, executor faults,
restart recovery and event replay. Chromium drives the installed UI at desktop
and narrow widths, including report access, retry, offline recovery, stale
responses and repeated navigation. Browser tooling is locked for Linux x86_64
and CPython 3.12. There are no frontend runtime dependencies or remote fonts.

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

The existing installed-package browser suite is preserved. An additional Chromium
suite creates videos through the actual HTTP service at 1440 and 390 CSS pixels,
opens the installed MP4, simulates a lost publication response, queries its
receipt, and verifies that reload and disabled resend preserve the same facts.
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
has no PWA or installer icon surfaces. Third-party JavaScript and Noto fonts are
bundled with their licenses and pinned SHA256 inventories in
`web-vendor.lock.json` and `web-fonts.lock.json`; there are no remote font or CDN
requests at runtime. Resource validation includes actual OpenType glyph coverage
for the shipped text. UI preview cards use document icons for fixture reports,
not fabricated video thumbnails.

`python scripts/check-ui-assets.py` checks the shipped assets. The installed
browser regression entry point now runs the existing fixture/media suites plus
native insecure-HTTP request identities, all twenty locales, preference failures
and synchronization, light/dark contrast, keyboard/RTL layouts, shared branding,
and double-click/lost-response/two-tab write conflicts. The non-loopback test uses
a constrained streaming proxy; production Host and Origin validation remains
unchanged. Browsers without any cryptographically secure randomness show a
recoverable error without sending an unkeyed write.
