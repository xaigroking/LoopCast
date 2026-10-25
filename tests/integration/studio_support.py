"""Explicit private test setup; no default service accounts or platform traffic."""
import hashlib
import json
import os
from pathlib import Path
from generation_support import settings


def configurations(directory):
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    generator = settings(root)
    provider = root / 'provider.toml'; provider.write_text('[app]\nopenai_api_key="contract-provider-canary"\n')
    generator['config_ref'] = str(provider)
    tools = Path(os.environ['LOOPCAST_MEDIA_TOOLS'])
    media = {key: str(tools / key) for key in ('ffmpeg', 'ffprobe')}
    media.update({key + '_sha256': hashlib.sha256((tools / key).read_bytes()).hexdigest() for key in ('ffmpeg', 'ffprobe')})
    profiles = {p: [{'stable_subject': 'test-' + p, 'label': p + ' contract account'}] for p in ('douyin','xiaohongshu','bilibili','youtube')}
    social = {'backend': 'contract', 'sending_enabled': True, 'timeout': 10, 'contract_profiles': profiles,
              'contract_results': {'test-xiaohongshu': 'lost_response', 'test-bilibili': 'not_sent', 'test-youtube': 'submitted'}}
    paths = []
    for name, value in [('generator', generator), ('media', media), ('social', social)]:
        path = root / (name + '.json'); path.write_text(json.dumps(value)); paths.append(path)
    return paths
