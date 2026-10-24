# LoopCast

A local, single-user fixture task console. Create a run, follow real execution
stages, cancel work, retry a failed attempt, and open its generated text report.
SQLite preserves history, request identities and event cursors across restarts.

**Fixture only:** no video generation, model calls, platform login or publishing.
This prototype binds to loopback and has no user authentication. It is intended
for one local user on Linux with Python 3.12 or newer.

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

A run freezes its input and keeps one set of logical stages: prepare, execute,
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
reports the available fixture capability. `/v1/runs` supports creation and
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
