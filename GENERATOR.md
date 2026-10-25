# Pinned generator installation and diagnostics

This operator workflow provisions the real MoneyPrinterTurbo source at
`82bb4a7502704559575f4e3d002b9dcb31d4ed6a`. It does not use the application's
contract-double wheel. It builds a real wheel and installs its complete default
runtime dependency set from that revision's `uv.lock` in a separate environment.
Optional upstream extensions outside the exposed creation settings are excluded.
Nothing is installed into the LoopCast Python environment or shared system
Python. The installer does not generate a video or contact a model provider.

The maintained installer supports Linux x86_64 and aarch64, with a provisioned
CPython 3.11–3.14. It checks the actual platform, executable tools, exact clean
Git revision, and hashes of the upstream project/lock/entry files. Supply a fresh
destination owned by the service operator. Failed installations retain a private
`installation.json` with completed steps; they are never silently overwritten.

## Prerequisites and installation

Use the installed LoopCast CLI. Provision Python and Git through the operating
system's normal administrative process. This command needs network access to
public source and package registries, downloads potentially large dependencies,
and consumes disk space; it does not configure the host package manager or Docker.

Obtain uv in a dedicated tools environment using the shipped exact hash lock.
The lock is also present at `loopcast/resources/mpt-tools.lock` in the installed
wheel. Use explicit absolute executable paths in the installer:

```sh
python3.12 -m venv /opt/loopcast-tools
/opt/loopcast-tools/bin/python -m pip install --require-hashes \
  -r src/loopcast/resources/mpt-tools.lock
sha256sum /opt/loopcast-tools/bin/uv

git clone --filter=blob:none --no-checkout \
  https://github.com/harry0703/MoneyPrinterTurbo.git /opt/mpt-source
git -C /opt/mpt-source checkout --detach 82bb4a7502704559575f4e3d002b9dcb31d4ed6a

python -I -m loopcast install-generator \
  --source /opt/mpt-source --destination /opt/mpt-runtime \
  --python /usr/bin/python3.12 --uv /opt/loopcast-tools/bin/uv \
  --uv-sha256 REPLACE_WITH_THE_VERIFIED_BINARY_SHA256 \
  --ffmpeg /opt/media-tools/ffmpeg --ffprobe /opt/media-tools/ffprobe --plan
```

The uppercase digest is deliberately nonfunctional. Check the actual tools before
substituting their hashes. Remove `--plan` to install. The command creates an
independent wheel-builder venv with `mpt-build.lock`, archives the pinned source
into the fresh destination, builds the upstream wheel without dependency
re-resolution, runs `uv sync --frozen --no-dev --no-install-project`, installs the
wheel without replacing the frozen dependency versions, checks dependencies and
runs CLI help. The installation record includes source/lock/builder/tool/wheel
hashes, interpreter and uv versions, package inventory and resource names.

Provision actual ffmpeg and ffprobe binaries and verify their version and license.
The repository's `scripts/install-media-tools.sh` is an optional explicitly invoked
Linux x86_64 static-tool installer with an archive hash. It installs only into
`LOOPCAST_MEDIA_TOOLS`; it does not change the system PATH. On aarch64 supply
compatible tools through the normal operator process. These tools are separate
licensed distributions; their inclusion in an environment does not change the
application license.

The real upstream wheel packages its resource fonts and media directories through
its own build manifest. Their source is the pinned upstream tree; inspect the
applicable font/media rights before use. The installer records which resources
are present rather than inventing a font source or downloading additional fonts.
The offline UI fonts have their own retained OFL notices and are separate from
video-renderer fonts. See the upstream `pyproject.toml` and LoopCast's third-party
inventory for the distribution boundaries.

## Private configuration and service binding

Installation emits `generator.json` and `media.json` with real absolute paths and
computed hashes, mode 0600. These files identify executable paths, the wheel and
media tools; `config_ref`, when added to `generator.json`, is a private TOML file
reference. It is not an API field, a media path or a provider key itself.

Copy one of the public examples to a private location and set its permissions:

```sh
install -m 600 examples/generation/provider.local.toml /etc/loopcast/provider.toml
```

Add the absolute `config_ref` path to the generated settings. The online example
contains empty values and requires authorized provider configuration. Never put
private settings or keys in a checkout, artifact, diagnostic output or issue.
The application accepts only declared provider fields with strict types and
bounded private configuration. Automatic upstream publishing stays disabled.

Bind the emitted configurations to the existing service:

```sh
python -I -m loopcast diagnose \
  --generator-config /opt/mpt-runtime/generator.json \
  --media-config /opt/mpt-runtime/media.json

python -I -m loopcast serve --data-dir /var/lib/loopcast/state \
  --generator-config /opt/mpt-runtime/generator.json \
  --media-config /opt/mpt-runtime/media.json
```

Append these flags to the existing systemd service only through the installation's
normal deployment process. The tool never rewrites a service unit. An invalid
optional generator configuration leaves the control plane running with generation
unavailable, so the settings diagnostics can explain the missing component.
Restart after changing the service-level JSON bindings.

## Offline composition and online paths

For local composition, import owned/licensed local video or images and optionally
your own narration audio. Enter a script, choose local material and either custom
audio or `no-voice`, and disable generated subtitles when no suitable local subtitle
path exists. The pinned MPT CLI composes with actual ffmpeg. This route can avoid
model, stock-media and TTS calls; verify the requested settings rather than
assuming every generation is offline. Existing scripts and materials are passed
as private managed copies, never arbitrary browser-supplied host paths.

Online script generation is an explicit action. Stock material collection requires
its configured provider and authorized media. Selecting an Edge voice invokes
online TTS. Other upstream voice/providers outside the service allowlist are not
implied to be supported. UI language preference does not select the video's
language: the creation settings carry the content language and voice separately.

Optional `network` uses `proxy_url` plus `allowed_proxy_hosts`. It is trusted local
configuration, not a normal form field. The model and material clients receive
explicit HTTP(S) proxy environment variables. The controlled child launcher passes
the proxy to the actual Edge `Communicate` constructor; it does not rely on
`aiohttp` reading ambient environment variables. Other TTS proxy combinations are
unsupported. This mapping is tested with a local transport and a strict Edge
interface double; it does not prove real remote connectivity or every SDK path.
Use existing access controls and private configuration for authenticated proxies.
The child omits publication credentials and all undeclared host environment.

## Diagnostics and evidence

The CLI above and the settings button call non-generating diagnostics. The API is
`GET /v1/studio/generator/diagnostics`. It checks recorded wheel identity/layout,
independent installed interpreter, CLI parameter support, private parseable
provider configuration, and pinned executable media tools. Checks return fixed
codes and next steps, not raw paths, keys, upstream output or exception messages.
It does not create a Run, publish, probe a paid provider or set a manual success flag.

`NOT_CONFIGURED`, `NOT_INSTALLED`, `INVALID` and `CALLABLE` distinguish setup states.
`CALLABLE` means local configuration and entry checks passed. Resource warnings
remain visible. Local composition, online model, stock media and TTS paths remain
`UNVERIFIED` until actual execution and review establish the relevant evidence.
A configured key, proxy or successful fixture is never treated as real production
verification. Actual runs retain revision/attempt/component identities and verified
media metadata; review the output, playback/download and restart persistence as
separate evidence.
