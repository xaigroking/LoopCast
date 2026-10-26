# Read-only channel observations

The existing application owner can inspect the local control plane, generator
installation, media tools and configured accounts. Reports separate component
configuration, declared capabilities, actual observations and live evidence.
A callable installation is not proof of generated video or published work.

Open the channel inventory with `GET /v1/operations/channels`. Reading the list,
a report or a check never starts a process or platform operation. New work is
explicit: `POST /v1/operations/channels/{channel_id}/checks` requires an
`Idempotency-Key` and `expected_version`. Requests are asynchronous and return a
stable check identity. Poll `GET /v1/operations/channel-checks/{check_id}`, then
read `GET /v1/operations/channel-reports/{report_id}`. A channel detail is available
at `GET /v1/operations/channels/{channel_id}`.

Local inspection reuses the existing generator diagnostics and runtime/media
observations. A fixed child process provides a total deadline and bounded output.
It can verify the pinned wheel, inspect CLI help and read tool versions. It never
runs generation, paid models, login, uploads or work queries.

Accounts default to stored observations and capability declarations. An online
identity check additionally requires `online: true`, `confirmed_online: true` and
trusted policy permission for that account. It reuses the original account-check
job and credential delivery. Active publication locks, unresolved publication and
authorization work cannot be taken over. Contract results remain fixture evidence.
Unknown transport or parsing outcomes are not proof of expired credentials.

Pass private policy JSON through `serve --inspection-config FILE`. Its format is
`loopcast.inspections.v1`; periodic inspection and live checks default off.
`online_accounts` explicitly lists account IDs and `allow_live` is required for
live identity checks. The initial report interface supports explicit checks;
periodic policy and transition history are delivered separately. No additional
service or publication schedule is created.

Reports retain the observed source, component and account version, time, safety
reason and limited evidence. Expired and superseded observations stay distinct
from failed checks. Freshness describes a bounded historical observation and is
not a continuous availability or sending guarantee. Old results cannot overwrite
new versions. Checks cannot release recovery guards, unresolved publication locks,
permanent media protection, attempt budgets or paused publication plans.

Schema v9 adds inspection state while preserving existing creation, feedback,
publication and schedule facts. Backup and runtime snapshot tools retain explicit
v6/v7/v8/v9 compatibility; reconciliation accepts v7/v8/v9. Controlled restores
interrupt old checks and retain the existing upload barrier and new execution
identity. There is no force-send or guard-clearing inspection operation.
