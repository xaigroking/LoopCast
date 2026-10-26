# Private consistency backups

Stop the application normally before backing up its data directory. The backup
command takes the same exclusive directory lock as the service and refuses an
active owner. It does not stop system services, kill processes or change the host.
This operator command currently requires Linux with `/proc` and `renameat2`.

```sh
python -I -m loopcast backup create \
  --data-dir /var/lib/loopcast/state \
  --output /private/backups/loopcast-new
```

The output directory must not exist. Keep the returned `manifest_sha256` in an
independent trusted private record. Verify a package against that recorded value:

```sh
python -I -m loopcast backup verify \
  --backup /private/backups/loopcast-new \
  --expected-manifest-sha256 RECORDED_MANIFEST_SHA256
```

The placeholder is deliberately unusable. A matching checksum verifies bytes
against the recorded package identity; it does not authenticate the author of an
untrusted package. These backups contain private business data and raw metric
imports. Store them privately, including when moving them to another machine.

The versioned manifest records the actual database schema, transaction-visible
counts and event boundary, installed Python code digest, declared distribution
version and each required file's relative name, size and digest. The source Git
revision and original wheel identity remain unavailable when no corresponding
build evidence is installed; the package version alone does not establish them.

The command uses SQLite's consistency backup API, including committed WAL data,
and copies every registered legacy artifact and media-library file. The database
preserves publication receipts, unknown attempts and locks, permanent media
families, creation revisions, original feedback bytes and decision links. Missing,
changed or corrupt required files cause failure. It rejects path traversal and
untrusted symbolic links. New packages use private permissions and become visible
only after validation and an atomic operation that cannot overwrite a destination.

Platform credentials, provider settings, login interactions, workspaces and
unregistered files are excluded. Keep authorized private configuration under its
separate operator backup policy. Recovering application facts does not recover
platform authorization, and reauthorizing never resets publication history.

Do not restore an older database by copying it over a current workspace. A backup
cannot know about external sends that happened after its boundary. A service that
starts from an old database has not thereby proved that sending is safe. Keep
newer facts and use the controlled recovery tooling for the compatible release.
Removing application binaries or rotating logs must retain state, permanent
publication records, required evidence and backups.

## Controlled recovery

Restore only to a new private directory, using the independently recorded digest:

```sh
python -I -m loopcast restore \
  --backup /private/backups/loopcast-new \
  --expected-manifest-sha256 RECORDED_MANIFEST_SHA256 \
  --data-dir /var/lib/loopcast/recovered-new

python -I -m loopcast reconcile --data-dir /var/lib/loopcast/recovered-new
```

The operator workflow accepts compatible v6/v7 backup databases and upgrades the
new directory to schema v7. It verifies the complete private package before
migration and publishes a usable directory only after the restore barrier is
durable. Failures do not overwrite the package, an existing destination or newer
application facts. Ordinary upgrades and restarts do not create a restore event.

Controlled recovery blocks all studio uploads, including explicit requests,
durable queued work and the final adapter call. Interrupted prepared/sending
attempts become recovery-uncertain and retain their account locks. Old execution
identities cannot accept late results. Existing published/unknown facts, receipts
and media families remain protected. Reauthorizing an account, changing the
sending configuration, selecting another evidence mode or waiting does not remove
the barrier. Credentials are excluded, so recovered accounts require authorization.

The service can remain ready for history, media, creation, feedback and diagnostics
while uploads are blocked. The fixed publication simulator continues to be a
simulator; its results cannot authorize a studio upload.

`reconcile` is a business read-only report of the recorded boundary, known targets
and receipts, missing coverage and declared query availability. It does not call a
platform or alter publication facts. The CLI cannot establish runtime adapter
readiness, so its query status is unavailable. Existing explicitly configured
per-target verification can still append a trusted receipt under current execution
identity; it cannot establish complete coverage of unknown works after the backup.

This release has no adapter capable of proving complete external coverage and no
command or HTTP switch for releasing the global barrier. A found target, empty
list, imported metrics or manual confirmation does not establish that coverage.
The report keeps `release_eligible=false` and explains the unbounded unknown scope.
Future release support requires a reliable coverage verifier and compatible
evidence semantics. Never clear a guard, lock or permanent record manually.

On schema v7, successful backups also try to record their identity, time and
manifest digest in the source database. The package is verified independently;
the audit field reports unavailable if that optional metadata cannot be recorded.
The audit contains no private destination path and is not a replacement for
verifying the actual package against its independently recorded digest.
