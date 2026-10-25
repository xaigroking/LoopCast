"""Strict, transport-independent creation, connection and release contracts."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

Id = Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{32}$')]
Text = Annotated[StrictStr, Field(max_length=120)]
Platform = Literal['douyin', 'xiaohongshu', 'bilibili', 'youtube']
EvidenceMode = Literal['live', 'contract']
Number = StrictInt | StrictFloat


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, json_schema_serialization_defaults_required=True)


class Scene(Model):
    id: Id
    narration: Annotated[StrictStr, Field(max_length=4000)]
    visual: Annotated[StrictStr, Field(max_length=1000)]
    duration: Annotated[Number, Field(gt=0, le=300)]


class CreationSettings(Model):
    language: Annotated[StrictStr, Field(max_length=20)] = 'en-US'
    voice: Annotated[StrictStr, Field(max_length=100)] = 'no-voice'
    voice_rate: Annotated[Number, Field(ge=0.5, le=2)] = 1.0
    aspect: Literal['9:16', '16:9', '1:1'] = '9:16'
    source: Literal['local', 'pexels', 'pixabay'] = 'local'
    material_ids: Annotated[list[Id], Field(max_length=30)] = Field(default_factory=list)
    parent_video_id: Id | None = None
    audio_id: Id | None = None
    music_id: Id | None = None
    music_volume: Annotated[Number, Field(ge=0, le=1)] = 0.2
    subtitles: StrictBool = True
    subtitle_position: Literal['top', 'center', 'bottom'] = 'bottom'
    subtitle_size: Annotated[StrictInt, Field(ge=12, le=120)] = 48
    clip_duration: Annotated[StrictInt, Field(ge=1, le=30)] = 5
    keywords: Annotated[list[Annotated[StrictStr, Field(max_length=80)]], Field(max_length=20)] = Field(default_factory=list)


class Draft(Model):
    title: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    brief: Annotated[StrictStr, Field(max_length=2000)] = ''
    script: Annotated[StrictStr, Field(max_length=20000)] = ''
    scenes: Annotated[list[Scene], Field(max_length=100)] = Field(default_factory=list)
    settings: CreationSettings = Field(default_factory=CreationSettings)


class ProjectCreate(Model):
    title: Annotated[StrictStr, Field(min_length=1, max_length=120)]


class DraftUpdate(Model):
    expected_version: Annotated[StrictInt, Field(ge=1)]
    draft: Draft


class VersionCommand(Model):
    expected_version: Annotated[StrictInt, Field(ge=1)]


class GenerateRequest(VersionCommand):
    stage: Literal['script', 'audio', 'materials', 'subtitle', 'video'] = 'video'


class Generation(Model):
    id: Id
    revision_id: Id
    run_id: Id
    stage: str
    input_digest: str
    component_revision: str
    component_sha256: str
    state: str
    asset_ids: list[Id]
    created_at: str


class Revision(Model):
    id: Id
    number: int
    draft: Draft
    input_digest: str
    created_at: str
    generations: list[Generation]


class Project(Model):
    id: Id
    version: int
    draft: Draft
    created_at: str
    updated_at: str
    revisions: list[Revision]


class ProjectList(Model):
    items: list[Project]


class MediaMetadata(Model):
    duration: float | None
    width: int | None
    height: int | None
    codec: str


class Asset(Model):
    id: Id
    name: str
    kind: Literal['video', 'audio', 'image', 'script', 'subtitle']
    media_type: str
    size: int
    sha256: str
    family_id: Id
    parent_id: Id | None
    origin: Literal['import', 'generation']
    source: str
    generation_id: Id | None
    attempt_id: Id | None
    metadata: MediaMetadata
    created_at: str


class AssetList(Model):
    items: list[Asset]


class PlatformCapability(Model):
    platform: Platform
    login_methods: list[Literal['browser', 'qr']]
    configured: bool
    verification: Literal['unavailable', 'contract_tested_live_unverified']
    video: bool
    query: bool
    schedule: bool
    ai_disclosure: bool
    thumbnail: bool
    title_limit: int
    requires_category: bool
    visibility_options: list[Literal['public', 'unlisted', 'private']]


class StudioCapabilities(Model):
    generation: bool
    media: bool
    sending_enabled: bool
    evidence_mode: EvidenceMode
    platforms: list[PlatformCapability]
    voices: list[str]
    sources: list[Literal['local', 'pexels', 'pixabay']]
    script_generation: bool


class Account(Model):
    id: Id
    platform: Platform
    stable_subject: str
    label: str
    tags: list[str]
    state: Literal['AVAILABLE', 'EXPIRED', 'DISCONNECTED', 'PENDING']
    version: int
    checked_at: str | None
    evidence_mode: EvidenceMode


class AccountUpdate(VersionCommand):
    label: Text
    tags: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=30)]], Field(max_length=10)]


class AccountList(Model):
    items: list[Account]


class AuthorizationCreate(Model):
    platform: Platform
    account_id: Id | None = None
    method: Literal['browser', 'qr'] = 'browser'


class AuthorizationCancel(Model):
    pass


class Authorization(Model):
    id: Id
    platform: Platform
    account_id: Id | None
    method: str
    state: Literal['PENDING', 'WAITING', 'VERIFYING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED']
    expires_at: float
    error_code: str | None
    version: int


class Interaction(Model):
    kind: Literal['click', 'text', 'key', 'refresh', 'confirm']
    x: Annotated[Number, Field(ge=0, le=1280)] | None = None
    y: Annotated[Number, Field(ge=0, le=800)] | None = None
    text: Annotated[StrictStr, Field(max_length=1000)] | None = None


class PlatformFields(Model):
    category: Annotated[StrictInt, Field(ge=1, le=10000)] | None = None
    visibility: Literal['public', 'unlisted', 'private'] = 'public'
    declaration: Annotated[StrictStr, Field(min_length=1, max_length=80)] | None = None


class ReleaseTargetInput(Model):
    account_id: Id
    fields: PlatformFields = Field(default_factory=PlatformFields)


class PreflightRequest(Model):
    asset_id: Id
    title: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    description: Annotated[StrictStr, Field(max_length=5000)] = ''
    cover_id: Id | None = None
    targets: Annotated[list[ReleaseTargetInput], Field(min_length=1, max_length=30)]


class TargetCheck(Model):
    account_id: Id
    platform: Platform
    allowed: bool
    reasons: list[str]
    account_version: int


class Preflight(Model):
    digest: str
    allowed: bool
    targets: list[TargetCheck]
    asset_sha256: str


class ReleaseCreate(PreflightRequest):
    preflight_digest: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
    confirmed: Literal[True]


class ReleaseReceipt(Model):
    id: Id
    created_at: str
    source: str
    outcome: str
    work_id: str | None
    evidence_code: str


class ReleaseAttempt(Model):
    id: Id
    state: str
    started_at: str
    ended_at: str | None


class ReleaseTarget(Model):
    id: Id
    account_id: Id
    platform: Platform
    stable_subject: str
    state: Literal['READY', 'PREPARED', 'SENDING', 'SUBMITTED', 'PUBLISHED', 'NOT_SENT', 'UNKNOWN', 'CANCELLED']
    version: int
    evidence_mode: EvidenceMode
    can_dispatch: bool
    can_verify: bool
    can_cancel: bool
    fields: PlatformFields
    asset_sha256: str
    attempts: list[ReleaseAttempt]
    receipts: list[ReleaseReceipt]


class ReleaseBatch(Model):
    id: Id
    asset_id: Id
    title: str
    description: str
    cover_id: Id | None
    input_digest: str
    state: str
    created_at: str
    targets: list[ReleaseTarget]


class ReleaseList(Model):
    items: list[ReleaseBatch]


class DiagnosticCheck(Model):
    id: str
    state: Literal['PASS','FAIL','WARN']
    code: str


class GeneratorDiagnostic(Model):
    state: Literal['NOT_CONFIGURED','NOT_INSTALLED','INVALID','CALLABLE']
    callable: bool
    live_verification: Literal['UNVERIFIED']
    checks: list[DiagnosticCheck]
    paths: dict[str, Literal['UNVERIFIED']]
    network: dict[str, str]
    supported_stages: list[str]
