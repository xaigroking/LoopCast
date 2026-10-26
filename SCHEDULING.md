# Finite local publication schedules

The installed storage layer supports one-time and finite daily-local plans for
existing frozen release targets. Automatic scheduled publication is disabled by
default. The current storage/time foundation does not start a scheduling worker
or expose schedule HTTP actions.

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
