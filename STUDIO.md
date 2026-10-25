# Creation and controlled publishing

The six workspace routes share the same brand, theme and 20 language packs.
`/create` owns editable drafts and immutable revisions; `/library` owns verified
media; `/accounts` owns connections and short-lived authorization sessions;
`/publish` freezes exact media and account targets before any send. `/` retains
the original run console and publication simulator.

Save a draft, select voice/materials/subtitles/aspect/music, and explicitly
request a generation stage. A generation freezes the revision and component
identity. Scene narration follows the saved order; visual descriptions provide
fallback search terms. Scene durations are authoring estimates with a one-hour
total limit. The upstream renderer uses its supported global material clip
duration, the actual voice track and its own composition behavior. The preview
timeline seeks within the probed output. Revisions and output files are retained.

Browser edit buffers and unresolved write identities survive navigation,
language/theme changes and refresh in the same browser tab. Server records remain
authoritative. Version conflicts preserve the local buffer for review; loading
the server draft is explicit. An uncertain write is retried only by an explicit
action using its original idempotency key. Authorization text controls are
ephemeral and never put in browser storage. A disabled browser storage facility
is reported, so drafts can be saved before leaving.

## Provisioning

Default startup makes no provider calls and has no seeded accounts, media or
authorization images. Add trusted, private configuration files to the backend
launch command as needed:

```sh
python -I -m loopcast serve --data-dir /var/lib/loopcast/state \
  --generator-config /etc/loopcast/generator.json \
  --media-config /etc/loopcast/media.json \
  --social-config /etc/loopcast/social.json
```

Generator configuration retains the documented MPT manifest, independent Python
environment, wheel SHA256, probe SHA256 and deadline. The supported upstream is
[MoneyPrinterTurbo at 82bb4a7502704559575f4e3d002b9dcb31d4ed6a](https://github.com/harry0703/MoneyPrinterTurbo/tree/82bb4a7502704559575f4e3d002b9dcb31d4ed6a).
Optional `voices` is an allowlist of supported voice identifiers. Provider
configuration is read from `config_ref`; the UI never receives that file or
selects executables. Only the declared provider fields are accepted. Local
scripts and materials need no LLM script generation. Script generation is a
separate user action and requires a configured provider. `--stop-at` uses the
actual `script`, `audio`, `subtitle`, `materials` and `video` CLI stages. Child
environments omit host credentials and force automatic publishing off.

A media configuration declares only `ffmpeg`, `ffmpeg_sha256`, `ffprobe` and
`ffprobe_sha256`. Use provisioned absolute binary paths and their verified hashes.
Imports send bytes with a filename and source/usage-rights description. They
accept at most 128 MiB, inspect actual media type, dimensions and duration, and
register immutable SHA256 and media metadata. Content is read through opaque IDs,
supports byte ranges, and is checked against its saved size and hash. User
requests never choose host filesystem paths.

The optional `network` object for generator/social execution accepts an explicit
`proxy_url` and `allowed_proxy_hosts`. The proxy host must be in that allowlist.
Only the generated proxy variables are passed on; the parent environment is not
copied. Keep authenticated proxy details in private service configuration.

## Platform connections

The supported source is
[social-auto-upload at 3847fff01f5434a8a32829833163cd6723cabc59](https://github.com/dreammis/social-auto-upload/tree/3847fff01f5434a8a32829833163cd6723cabc59).
Provision its dependencies in a separate Python environment. A live configuration
uses `backend: "sau"` and declares `python`, `source_root`, `source_revision`,
`source_sha256`, `browser_path`, `browser_sha256`. Obtain `source_sha256` from
`loopcast.social.source_digest(Path(source_root))` in the installed application.
It hashes the Python tree with filenames and separators, excluding `.git` and
`__pycache__`. The pinned revision is an operator attestation; the hash detects
subsequent source drift. Keep the component checkout clean.

Set `sending_enabled` explicitly to `true` only when controlled live operation
has been authorized. It defaults to false and cannot be enabled through the UI.
`timeout` is a bounded execution deadline, 5–600 seconds. Bilibili additionally
requires a provisioned `biliup` path and `biliup_sha256`; the adapter overrides
the upstream automatic binary download.

| Platform | Login | Server identity check | Video result | Read-only result lookup |
| --- | --- | --- | --- | --- |
| Douyin | Controlled browser / QR | Explicit own-account response probe | Submitted | Unavailable |
| Xiaohongshu | Controlled browser / QR | Explicit own-account response probe | Submitted | Unavailable |
| Bilibili | Interactive QR via private PTY | Authenticated first-party `mid` | Submitted | Unavailable |
| YouTube | Controlled browser | Authenticated Studio channel identity | Submitted | Unavailable |

Douyin and Xiaohongshu require a verified `identity_probes` entry before their
capability becomes configured. Its keys are `url`, `id_path`, `label_path`,
`success_path`, `success_value`. The URL must be a first-party HTTPS own-account
response route on `creator.douyin.com` or `creator.xiaohongshu.com` respectively,
without credentials, query or fragment. JSON paths are bounded arrays of field
names; the selected identity field must be a stable subject, not a nickname.
The adapter observes the site's authenticated response, checks the exact success
type/value, and reads the identity server-side. There is no assumed undocumented
endpoint or API accepting a user-asserted live subject. Provisioning these probes
and validating actual platform flows remains an operator task.

Browser login uses a pinned headless browser with an owner-scoped screenshot,
click, text and fixed-key interface. Password fields are masked. Keyboard
navigation makes the controls reachable without a pointer. Bilibili uses the
actual native QR image and arrow/Enter controls. Screens and controls expire
with their session. Cancelled, expired, restarted or mismatched-identity sessions
cannot replace credentials. Same-account login updates the credential reference
and preserves its stable account ID and publishing history.

Credentials are private files with opaque references, not media or API fields.
Copies are granted only to the isolated job and removed afterward. Disconnecting
removes the local credential reference while retaining identity and history.
The owner cookie isolates authorization sessions; this is still a single-user
application behind deployment access controls, not a cloud authentication system.

## Release evidence and identity

Preflight verifies media integrity, account identity/state/check freshness
(five minutes), platform-specific fields and the permanent guard. Bilibili
requires a category; YouTube exposes its supported visibility values; Douyin
accepts exact platform AI-declaration text. Scheduling is unavailable. The
capability matrix controls each field and action.

Confirmation freezes title, description, cover, content digest and exact
target set. Each target has its own version, attempt token, lease and receipts.
A single publication owner drains durable jobs with per-account locks.
Only an explicit per-target action starts a send. Batch status is derived from
all targets: partial success and unresolved outcomes remain visible.

The four pinned upload interfaces return credential paths or submission status.
An exit code of zero therefore yields `SUBMITTED`, never confirmed publication.
No read-only work query is implemented by those pinned interfaces, so live
lookup is unavailable and the result remains unresolved. Timeouts, lost responses
and interrupted sends become `UNKNOWN`; they do not automatically resend or
release an unresolved account lock. Late retired tokens cannot change results.
A process crash cannot prove the previous sender exited; recovery conservatively
retains this ambiguity. A new intent cannot override it.

The permanent guard is keyed by platform, verified stable subject, media family
and evidence mode. Exact SHA256 copies share a family. Declared derivatives
merge into the original family. Duration plus five sampled visual fingerprints
conservatively match common transcodes; these are duplicate-detection heuristics,
not a proof that arbitrary edited media are independent. Declare the original
for copies, edits and transcodes. A new family requires new content that neither
matches existing media nor declares an existing parent. Project/run names,
metadata edits, cookie replacement, restart and time do not reset the guard.
There is no expiry, force-send or reason-based override.

Old simulator receipts stay in the original run history. The optional
`backend: "contract"` is an explicit isolated testing configuration: profiles
and result scenarios must be supplied by the test harness. Its evidence is
permanently marked `contract` and cannot satisfy a live account's publication
facts. It is never a default source of production accounts or successful sends.

## Verification and distribution

`scripts/check-tests.sh` tests installed wheels, migrations, strict contracts,
authorization isolation, concurrency, crash fencing, staged outputs, receipt
classification, lineage and duplicate guards. `scripts/check-web.sh` installs
the release wheel and extracted standalone frontend outside the source tree,
then exercises original workflows and creation-to-publishing with isolated
fixtures. It checks video/audio outputs, playback, draft preservation, target
states, all 20 locales, RTL and 1440/390 layouts. CI performs no real account login
or live send.

`scripts/build-release.sh` requires committed source, checks generated API types,
and emits both artifacts with a shared source revision and SHA256 manifest.
License checks inspect the delivered wheel and standalone archive, including
original Apache-2.0 notices and retained third-party notices.
