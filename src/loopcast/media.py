"""Bounded media probing, immutable storage and conservative video fingerprints."""
import hashlib
import json
from pathlib import Path
import shutil
from .domain import Problem, uid
from .runner import run, RunnerError

LIMIT = 128 * 1024 * 1024
EXTENSIONS = {'video/mp4': '.mp4', 'video/webm': '.webm', 'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg',
              'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'text/plain; charset=utf-8': '.txt', 'application/x-subrip': '.srt'}


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''): value.update(chunk)
    return value.hexdigest()


class MediaLibrary:
    def __init__(self, store, settings=None):
        self.store = store
        self.root = store.directory / 'library'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.settings = settings
        if settings:
            if set(settings) != {'ffmpeg', 'ffmpeg_sha256', 'ffprobe', 'ffprobe_sha256'}:
                raise ValueError('Invalid media configuration')
            self.ffmpeg = Path(settings['ffmpeg']).resolve(strict=True)
            self.ffprobe = Path(settings['ffprobe']).resolve(strict=True)
            self.verify_tools()

    def verify_tools(self):
        if not self.settings:
            raise Problem('CAPABILITY_UNAVAILABLE', 'Media tools are not configured.', 422)
        if file_digest(self.ffmpeg) != self.settings['ffmpeg_sha256'] or file_digest(self.ffprobe) != self.settings['ffprobe_sha256']:
            raise Problem('COMPONENT_UNAVAILABLE', 'Media tools changed after configuration.', 409)

    def path(self, identity):
        info = self.store.studio_asset_file(identity)
        path = self.root / info['storage_ref']
        if path.is_symlink() or path.resolve().parent != self.root.resolve() or not path.is_file():
            raise Problem('ARTIFACT_UNAVAILABLE', 'Asset is not available.', 409)
        if path.stat().st_size != info['size'] or file_digest(path) != info['sha256']:
            raise Problem('ARTIFACT_UNAVAILABLE', 'Asset integrity verification failed.', 409)
        return path

    def inspect(self, path, kind, cancelled=lambda: False):
        self.verify_tools()
        if not 0 < path.stat().st_size <= LIMIT:
            raise Problem('INVALID_MEDIA', 'Use a nonempty asset of at most 128 MiB.', 400)
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        try:
            info = json.loads(run([str(self.ffprobe), '-v', 'error', '-protocol_whitelist', 'file', '-show_streams', '-show_format', '-of', 'json', str(path)], self.root, env, 15, cancelled))
            streams = info.get('streams', [])
            formats = info['format']['format_name'].split(',')
            video = next((s for s in streams if s['codec_type'] == 'video'), None)
            audio = next((s for s in streams if s['codec_type'] == 'audio'), None)
            if video and not (0 < int(video['width']) <= 4096 and 0 < int(video['height']) <= 4096): raise ValueError()
            fingerprint = None
            if kind == 'video':
                if not video or not set(formats).intersection({'mov', 'mp4', 'matroska', 'webm'}): raise ValueError()
                duration = float(info['format']['duration'])
                if not 0 < duration <= 3600: raise ValueError()
                mime = 'video/webm' if 'webm' in formats else 'video/mp4'
                fingerprint = self.fingerprint(path, duration, cancelled)
            elif kind == 'audio':
                if not audio or video: raise ValueError()
                duration = float(info['format']['duration'])
                if not 0 < duration <= 3600: raise ValueError()
                mime = {'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'ogg': 'audio/ogg'}.get(formats[0], 'audio/mp4')
            elif kind == 'image':
                if not video or video['codec_name'] not in {'png', 'mjpeg', 'webp'}: raise ValueError()
                duration = None
                mime = {'png': 'image/png', 'mjpeg': 'image/jpeg', 'webp': 'image/webp'}[video['codec_name']]
            else: raise ValueError()
            if video and not (0 < int(video['width']) <= 4096 and 0 < int(video['height']) <= 4096): raise ValueError()
            metadata = {'duration': duration, 'width': int(video['width']) if video else None, 'height': int(video['height']) if video else None, 'codec': (video or audio)['codec_name']}
            return mime, metadata, fingerprint
        except (ValueError, KeyError, TypeError, RunnerError):
            raise Problem('INVALID_MEDIA', 'The media probe rejected this asset.', 400) from None

    def fingerprint(self, path, duration, cancelled):
        frames, colors = [], []
        for part in (.1, .3, .5, .7, .9):
            raw = run([str(self.ffmpeg), '-v', 'error', '-protocol_whitelist', 'file,pipe', '-ss', str(max(0, duration * part - .1)), '-i', str(path), '-frames:v', '1', '-vf', 'scale=9:8', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], self.root,
                      {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, 5, cancelled, limit=4096)
            if len(raw) != 9 * 8 * 3: raise RunnerError('INVALID_MEDIA')
            brightness = [sum(raw[i:i+3]) for i in range(0, len(raw), 3)]
            value = 0
            for row in range(8):
                for col in range(8): value = (value << 1) | (brightness[row*9+col] > brightness[row*9+col+1])
            frames.append(f'{value:016x}')
            colors.extend(round(sum(raw[channel::3]) / 72 / 16) for channel in range(3))
        return {'duration': duration, 'frames': frames, 'colors': colors}

    def register(self, path, kind, name, source, key, *, parent_id=None, generation_id=None, attempt=None, cancelled=lambda: False):
        path = Path(path)
        if kind in {'script', 'subtitle'}:
            data = path.read_bytes()
            if not 0 < len(data) <= 1048576: raise Problem('INVALID_MEDIA', 'Text result is too large.', 400)
            data.decode('utf-8')
            mime = 'application/x-subrip' if kind == 'subtitle' else 'text/plain; charset=utf-8'
            metadata = {'duration': None, 'width': None, 'height': None, 'codec': 'utf-8'}
            fingerprint = None
        else:
            mime, metadata, fingerprint = self.inspect(path, kind, cancelled)
        identity = uid()
        destination = self.root / (identity + EXTENSIONS[mime])
        try:
            with path.open('rb') as origin, destination.open('xb') as target:
                destination.chmod(0o600)
                shutil.copyfileobj(origin, target)
            value = {'id': identity, 'name': name[:120], 'kind': kind, 'media_type': mime, 'size': destination.stat().st_size,
                     'sha256': file_digest(destination), 'storage_ref': destination.name, 'metadata': metadata, 'fingerprint': fingerprint,
                     'origin': 'generation' if generation_id else 'import', 'source': source[:500]}
            result, created = self.store.studio_register_asset(value, key, parent_id, generation_id, attempt)
            if not created: destination.unlink(missing_ok=True)
            return result
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
