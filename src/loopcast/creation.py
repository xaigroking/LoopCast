"""Map frozen creation inputs to the pinned MPT public CLI and verified outputs."""
import json
from pathlib import Path
import shutil
import time
import uuid
from .domain import Problem
from .generation import config_text
from .media import EXTENSIONS
from .runner import run, RunnerError
from .diagnostics import child_context, log_event


class CreationExecutor:
    def __init__(self, generator, library):
        self.generator, self.library = generator, library

    def execute(self, recipe, work, attempt, cancelled):
        generator = self.generator
        generator.verify()
        draft, stage = recipe['draft'], recipe['stage']
        options = draft['settings']
        if options['voice'] not in generator.voices:
            raise RunnerError('VOICE_UNAVAILABLE')
        work = Path(work).resolve()
        work.mkdir(mode=0o700, parents=True, exist_ok=False)
        config = work / 'config.toml'
        config.touch(mode=0o600)
        context = child_context(attempt['trace'])
        fields = {'run_id': attempt['run_id'], 'attempt_id': attempt['id'], 'revision_id': recipe['revision_id'], 'project_id': recipe['project_id']}
        log_event('generation', 'creation.started', context, **fields)
        result_state = 'FAILED'
        try:
            config.write_text(config_text(generator.config_ref))
            task_id = str(uuid.uuid4())
            script = '\n\n'.join(s['narration'] for s in draft['scenes']) if draft['scenes'] else draft['script']
            args = {
                'video-subject': draft['title'], 'video-script': script, 'video-script-prompt': draft['brief'],
                'video-language': options['language'], 'video-source': options['source'], 'video-aspect': options['aspect'],
                'video-count': 1, 'voice-name': options['voice'], 'voice-rate': options['voice_rate'], 'video-clip-duration': options['clip_duration'],
                'subtitle-position': options['subtitle_position'], 'font-size': options['subtitle_size'], 'bgm-volume': options['music_volume'],
                'bgm-type': 'custom' if options['music_id'] else 'none', 'stop-at': stage, 'task-id': task_id,
            }
            terms = options['keywords'] or [s['visual'] for s in draft['scenes'] if s['visual'].strip()]
            if terms: args['video-terms'] = ','.join(terms)
            local = []
            def copy_asset(identity, directory):
                asset = self.library.store.studio_asset(identity)
                source = self.library.path(identity)
                destination = work / directory / (identity + EXTENSIONS[asset['media_type']])
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                return destination
            for identity in options['material_ids']:
                if self.library.store.studio_asset(identity)['kind'] not in {'video', 'image'}: raise RunnerError('INVALID_MATERIAL')
                local.append(str(copy_asset(identity, 'inputs')))
            if local: args['video-materials'] = ','.join(local)
            for field, flag, directory in [('audio_id', 'custom-audio-file', 'inputs'), ('music_id', 'bgm-file', 'storage/bgm')]:
                if options[field]:
                    if self.library.store.studio_asset(options[field])['kind'] != 'audio': raise RunnerError('INVALID_MATERIAL')
                    args[flag] = str(copy_asset(options[field], directory))
            argv = [generator.python, '-I', '-c', 'from app.launcher import main; raise SystemExit(main())', 'cli']
            argv.extend('--' + name + '=' + str(value) for name, value in args.items())
            argv.append('--subtitle-enabled' if options['subtitles'] else '--no-subtitle-enabled')
            raw = run(argv, work, generator.environment(work) | generator.network, generator.timeout, cancelled)
            try:
                value = json.loads(raw)
                if value['task_id'] != task_id or not isinstance(value['result'], dict): raise ValueError()
                result = value['result']
                required = {'script': 'script', 'audio': 'audio_file', 'materials': 'materials', 'subtitle': 'subtitle_path', 'video': 'videos'}[stage]
                if not result.get(required): raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise RunnerError('INVALID_RESULT') from None
            outputs = []
            if isinstance(result.get('script'), str) and result['script'].strip():
                path = work / 'script.txt'; path.write_text(result['script'])
                outputs.append((path, 'script'))
            for key, kind in [('audio_file', 'audio'), ('subtitle_path', 'subtitle')]:
                if result.get(key): outputs.append((self.output_path(result[key], work), kind))
            for key in ('materials', 'videos'):
                values = result.get(key, [])
                if not isinstance(values, list) or len(values) > 30: raise RunnerError('INVALID_RESULT')
                for name in values:
                    path = self.output_path(name, work)
                    kind = 'image' if path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'} else 'video'
                    outputs.append((path, kind))
            if not outputs: raise RunnerError('INVALID_RESULT')
            assets = []
            for index, (path, kind) in enumerate(outputs):
                parent = options['parent_video_id'] if kind == 'video' else None
                asset = self.library.register(path, kind, draft['title'] + ' · ' + stage,
                    'MoneyPrinterTurbo@' + generator.manifest['revision'], attempt['id'] + ':' + str(index),
                    parent_id=parent, generation_id=recipe['generation_id'], attempt=(attempt['run_id'], attempt['token']), cancelled=cancelled)
                assets.append(asset)
            result_state = 'SUCCEEDED'
            return assets
        finally:
            config.unlink(missing_ok=True)
            log_event('generation', 'creation.completed', context, result=result_state, **fields)

    @staticmethod
    def output_path(name, work):
        if not isinstance(name, str): raise RunnerError('INVALID_RESULT')
        path = Path(name)
        if not path.is_absolute(): path = work / path
        if not path.resolve().is_relative_to(work) or any(p.is_symlink() for p in [path, *path.parents] if p != work.parent):
            raise RunnerError('INVALID_ARTIFACT')
        if not path.is_file(): raise RunnerError('INVALID_ARTIFACT')
        return path
