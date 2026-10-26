# Manual observations and next-round decisions

The publishing center links to `/feedback`. This version accepts one explicit
public JSON contract, available at `/v1/feedback/schema`; the empty template is
at `/v1/feedback/template`. It does not claim automatic platform CSV ingestion or
background scraping. Prepare a manual export using the schema, preserving its
source, account/work/media mapping, metric definitions and timestamps.

Before accepting a row, bind its platform work explicitly to a server-verified
stable account and an exact registered video ID. Optionally associate a submitted,
unknown or published release target. Confirmed work IDs must match their existing
receipts; another ID cannot override them. External works and unresolved targets
are recorded as `MANUAL_ASSOCIATION`, not verified publication. A binding inherits
the account's `live` or `contract` evidence mode, which a request cannot override.
Work identities and their original mappings remain immutable.

Manual association or imported metrics never change the publication ledger,
clear account locks, mark UNKNOWN as published or bypass permanent duplicate
protection. Old simulator receipts cannot establish live work bindings.

## Import contract

Upload UTF-8 JSON, at most 1 MiB and 500 rows. `format` is
`loopcast.metrics.v1`. Each row specifies platform, stable subject, platform work
ID, exact asset ID, observation timestamp with timezone offset, aggregation,
`platform_reported_v1` definition, and observed metrics. The definition records
platform-reported views, likes, comments, shares and watch seconds as manually
supplied counters; it does not make different platforms analytically equivalent.
Count fields are integers, watch seconds may be fractional, and null/missing
values remain missing. Negative, nonfinite, coerced and out-of-range values are
rejected. Publication and observation times retain a clear source; observations
retain their original timezone offset and normalized UTC time.

Cumulative observations form immutable snapshots. Deltas require start/end
intervals with timezone offsets; overlapping intervals are quarantined. The UI
displays snapshots individually and does not silently sum cumulative counters.
The original uploaded bytes, filename, SHA256 and source are preserved privately
in the database and available as an attachment with integrity checking.

Exact-file replay returns the same import. Different exports of the same
observation do not double-count it. Conflicting values, decreasing cumulative
counters, invalid observation times, missing metrics, mismatched media and
unbound works receive explicit per-row quarantine reports. Structural/type errors
reject the document before mutation. A transaction commits only valid snapshots
and its complete report; unexpected failures roll back the operation.

After an explicit binding resolves a missing mapping, reprocess the import using
its current version. Original bytes and accepted observations stay unchanged.
Report versions preserve a trace of processing decisions. Conflicts cannot be
resolved by replacing historical snapshots or guessing a title match. Correct
observations require their own valid identity and source; there is no generic
force-import switch.

## Versioned baseline

The first rule version is `manual-observation-v1`. The user selects specific
snapshots from one platform and evidence mode, a views goal, minimum work age and
minimum observed views. The rule keeps the latest selected observation per work,
requires cumulative views, applies the age/sample thresholds, records exclusions
and references the eligible work with the highest observed views. Every decision
retains its selected IDs, immutable snapshot digests, parameters, reason, source
mode and strategy version. Missing/immature/insufficient data yields the recorded
`INSUFFICIENT_DATA` manual fallback.

The proposed topic and brief are reviewed and editable. Explicit confirmation
creates a frozen creation revision with a permanent decision reference. Later
confirmations append revisions under version checks; the original proposal and
observations do not change. Ordinary creation revisions retain their originating
decision link. The creator links back to the source snapshots. No model call or
send happens when proposing or confirming a decision; those remain separate
explicit creation/publishing actions.

This is a transparent manual baseline, not an experiment or proof of traffic
improvement. User-supplied publication dates and counters retain that provenance.
Real metric collection, account verification and performance evaluation require
separate evidence.

## Repeatable fixture demonstration

With installed media tools, explicitly run:

```sh
python -I -m loopcast demo-feedback \
  --data-dir /tmp/loopcast-feedback-fixture \
  --media-config /opt/mpt-runtime/media.json
```

The data directory must not exist. This operator-invoked tool creates two short
color videos with ffmpeg, an isolated contract account, contract-only publication
receipts, synthetic observations, a first-round decision and its second-round
creation revision/video/observation. It repeats an import to verify identity.
It makes no real platform or model calls and never seeds the normal application.
Output is explicitly `fixture_only_not_live_evidence`. Synthetic counts and ages
illustrate provenance; their change proves no performance benefit. Inspect the
workspace by serving that fixture directory with the explicit contract settings
if desired; the tool itself prints all relevant IDs and hashes. Do not bind a
fixture directory to a live sending configuration.

Schema v6 adds only the feedback tables and revision links. Older application
binaries reject that schema; retain the database and use a compatible forward
version instead of restoring a pre-feedback snapshot over new facts.
