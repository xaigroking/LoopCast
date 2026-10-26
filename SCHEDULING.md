# Finite local publication schedules

The installed storage layer supports one-time and finite daily-local plans for
existing frozen release targets. Automatic scheduled publication is disabled by
default. The existing publication owner advances due slots when a trusted schedule
policy is explicitly enabled. Manage finite plans in the publishing workspace.

Each daily date has a distinct, explicitly selected group of frozen targets.
A daily plan never repeats one video indefinitely or creates new content. Slot
identity belongs to the plan and nominal date, independently of revisions,
worker restarts or polling frequency. One-time plans have one fixed slot.

Time rules use an IANA zone and retain the measured TZif bytes and digest.
A repeated local time selects the earlier UTC instant once. A missing one-time
local time is rejected; a missing daily time skips that group without moving it
to the next day. Rules also cover half-hour changes and skipped calendar days.
Updating host time-zone data does not rewrite a confirmed plan's pinned rules.

Plans start paused. Immutable revisions, short-lived previews, explicit
confirmation, request identity and version comparisons protect changes. Dates,
time zone and target groups are fixed at creation. Changes to future times or
limits require a paused plan, a new preview and confirmation; existing
occurrences remain unchanged. Cancellation retains history and publication
facts. Reusing a reliably unsent original target requires a new explicit plan
and the same permanent media/account checks.

Schema v8 preserves previous creation, feedback and publication facts. Backup
and read-only snapshot tools accept v6, v7 and v8; reconciliation accepts v7 and
v8. A controlled restore pauses active plans and retains the upload guard and
new execution epoch before any background worker starts. Older executables that
cannot read v8 must not replace the database with an older copy.

Local delayed sending is distinct from platform-native scheduling: a platform's
`schedule=false` capability continues to mean native scheduling is unavailable.
No plan grants permission to bypass credentials, permanent media protection,
unresolved publication evidence or a recovery guard.

## Trusted execution policy

Pass a private JSON file with `serve --schedule-config FILE`. Its fixed format is
`loopcast.scheduling.v1`. `enabled` and `allow_live` both default to false. Live
execution also requires the existing social sending switch, matching account
and evidence identity, valid credentials, frozen media, platform capabilities,
permanent protection and a clear restore guard. No implicit login or generation
occurs. Platform-native scheduling remains unavailable.

Limits include active plans, finite slots, pending targets, plan/account attempts
per UTC day and bounded catch-up age/count. Pending authorization includes future
confirmed targets, including paused plans. Cancel an unused plan to release its
reliably unsent authorizations. Midnight does not reset a permanent media guard.

Each target reserves budget before entering the durable publication queue.
Final admission rechecks the current UTC day and policy and consumes the attempt.
A proven pre-admission cancellation releases a reservation; consumed, unknown or
submitted attempts are never refunded into a new send permission. Legacy attempt
time evidence contributes a conservative baseline. Incomplete legacy time
history blocks admission rather than becoming zero usage.

Default missed slots are skipped after a 60-second normal execution window.
Explicit bounded catch-up has both age and count limits stored across scan pages
and restarts. Account/capability/quota waits retain the original deadline. A
backward clock change beyond five seconds blocks new admission until time catches
up with its durable high-water mark. Network execution never runs in a SQLite
transaction.

Pause and cancel revoke unadmitted jobs. An operation already handed to the
adapter remains in flight and may complete; its actual result is retained even
when the plan is paused or cancelled. Unknown results keep the original target
and account lock. A reliable NOT_SENT requires explicit confirmation to retry
the original occurrence and remains subject to the shared attempt budget.

## Publishing workspace

First freeze a video and its publication targets using the existing publishing
preflight. Under **Publication plans**, choose eligible frozen targets. A
one-time plan uses one group; a finite daily plan uses the chosen group order,
one date per group. Review every nominal local date/time, IANA zone, UTC instant,
missed-slot policy and budget before saving. Saving always creates a paused plan.
Review that plan again and explicitly enable it when service policy permits.

An ambiguous write response retains its original request identity. Reloading
reads state; it never automatically repeats a write. Resolve the pending action
before changing its payload. Version conflicts retain your draft. Pause before
editing future times or limits; saving a revision keeps the plan paused until
another review and confirmation. Already materialized occurrences are immutable.

The history separates waiting, missed, skipped, in-flight, unresolved, partial
and published states. Each occurrence links to the original frozen release,
target, attempts and receipts, including releases older than the initial list.
An ended slot is not proof of publication. Browser language does not change the
plan's time zone or the content language.

## HTTP management

The strict OpenAPI schema describes:

- `GET /v1/studio/schedules/capabilities` and `POST /v1/studio/schedules/preview`.
- `GET/POST /v1/studio/schedules` and `GET/PUT /v1/studio/schedules/{id}`.
- `POST /v1/studio/schedules/{id}/pause`, `/resume`, `/cancel`.
- `GET /v1/studio/schedules/{id}/occurrences`.

Writes require `Idempotency-Key`; modifications and control actions also require
`expected_version`. Create and resume require literal `confirmed: true` and a
current preview/confirmation digest. Preview pages do not create business slots.
List cursors are scoped, signed and bounded to the current owner session; reload
the list after an owner restart. Persisted write identities do not depend on
preview or list tokens. There are no force, ledger-delete,
credential-upload, arbitrary command or native-platform scheduling endpoints.
