"""Private input identities and public, allowlisted applicability scopes."""
import hashlib
import json
from pathlib import Path
import uuid
from .domain import canonical

NAMESPACE = uuid.UUID('87f9beb1-f0ce-40c7-b24a-ac0f11c7637c')


def channel_id(kind, account_id=None):
    return uuid.uuid5(NAMESPACE, kind + ':' + (account_id or '')).hex


def file_identity(value):
    if not isinstance(value,str):return None
    try:
        path=Path(value);st=path.stat()
        return [str(path.absolute()),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
    except (OSError,ValueError):return ['unavailable',value]


def descriptor(kind, application, generator, media, social, account=None):
    settings={'CONTROL_STORAGE':None,'GENERATOR':generator,'MEDIA':media,'ACCOUNT':social.settings}[kind]
    configured=settings if isinstance(settings,dict) else {}
    refs={k:file_identity(v) for k,v in configured.items() if k in {'wheel','python','config_ref','ffmpeg','ffprobe','browser_path','biliup'}}
    private={'kind':kind,'settings':settings,'files':refs,'application':application}
    if kind=='ACCOUNT':
        private['account']={k:account[k] for k in ('id','platform','stable_subject','evidence_mode','credential_ref','label','tags')}
        private['credential']=file_identity(str(social.credentials/(str(account['credential_ref'])+'.json')))
        source=configured.get('source_root')
        if isinstance(source,str):
            # This is only an inventory marker. The existing adapter verifies bytes at execution.
            private['source_root']=file_identity(source)
    manifest=configured.get('manifest',{}) if kind=='GENERATOR' else {}
    def hex_value(value,length):
        return value if isinstance(value,str) and len(value)==length and all(c in '0123456789abcdef' for c in value) else None
    scope={'kind':kind,'account_id':account['id'] if account else None,'platform':account['platform'] if account else None,
           'account_version':account['version'] if account else None,'application_source':application.get('build_source',{}).get('value'),
           'installed_code_sha256':hex_value(application.get('installed_code_sha256',{}).get('value'),64),
           'component_revision':hex_value(manifest.get('revision') if isinstance(manifest,dict) else None,40),
           'artifact_sha256':hex_value(manifest.get('artifact_sha256') if isinstance(manifest,dict) else None,64),
           'evidence_mode':account['evidence_mode'] if account else social.mode}
    if not isinstance(scope['application_source'],str) or len(scope['application_source'])>80:scope['application_source']=None
    return {'id':channel_id(kind,account['id'] if account else None),'kind':kind,'account_id':account['id'] if account else None,
            'fingerprint':hashlib.sha256(canonical(private).encode()).hexdigest(),'scope':scope}
