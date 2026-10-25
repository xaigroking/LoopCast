#!/usr/bin/env bash
set -euo pipefail
# Public static GPL tools used only by optional generation checks on Linux x86_64.
if [[ $(uname -sm) != 'Linux x86_64' ]]; then
  echo 'Media checks require Linux x86_64' >&2; exit 1
fi
: "${LOOPCAST_MEDIA_TOOLS:?Set LOOPCAST_MEDIA_TOOLS to an external tools directory}"
mkdir -p "$LOOPCAST_MEDIA_TOOLS"
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
curl -fsSL --retry 2 https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o "$archive"
printf '%s  %s\n' abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67 "$archive" | sha256sum -c -
tar -xJf "$archive" -C "$LOOPCAST_MEDIA_TOOLS" --strip-components=1
"$LOOPCAST_MEDIA_TOOLS/ffprobe" -version | head -1
