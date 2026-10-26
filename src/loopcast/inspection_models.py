"""Finite, source-scoped read-only channel observations."""
from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator, field_validator
from .studio_models import Model, Id

Positive = Annotated[StrictInt, Field(ge=1)]
Code = Annotated[StrictStr, Field(pattern=r'^[A-Z][A-Z0-9_]{0,79}$')]
Result = Literal['NOT_CONFIGURED','MISCONFIGURED','UNVERIFIED','UNSUPPORTED','CALLABLE','FAILED','UNKNOWN']
CheckState = Literal['QUEUED','RUNNING','COMPLETED','INTERRUPTED','TIMED_OUT','SUPERSEDED']
Freshness = Literal['NOT_CHECKED','FRESH','EXPIRED','SUPERSEDED']
Kind = Literal['CONTROL_STORAGE','GENERATOR','MEDIA','ACCOUNT']
Evidence = Literal['source_inspected','fixture_verified','account_verified','unsupported','unverified']
ScopeKind = Literal['LOCAL_RUNTIME','COMPONENT_INSTALL','ACCOUNT_IDENTITY','CAPABILITY_DECLARATION']
Source = Literal['runtime.ready','operations.snapshot','generator.diagnostics','media.version','account.state','social.capabilities','social.check','inspection.executor']


class InspectionSettings(Model):
    format: Literal['loopcast.inspections.v1'] = 'loopcast.inspections.v1'
    enabled: StrictBool = False
    allow_live: StrictBool = False
    interval_seconds: Annotated[StrictInt, Field(ge=60, le=604800)] = 86400
    timeout_seconds: Annotated[StrictInt, Field(ge=5, le=60)] = 30
    concurrency: Literal[1] = 1
    max_pending: Annotated[StrictInt, Field(ge=1, le=32)] = 16
    freshness_seconds: Annotated[StrictInt, Field(ge=60, le=604800)] = 86400
    backoff_initial_seconds: Annotated[StrictInt, Field(ge=60, le=3600)] = 60
    backoff_max_seconds: Annotated[StrictInt, Field(ge=60, le=604800)] = 86400
    online_accounts: Annotated[list[Id], Field(max_length=253)] = []
    reports_per_channel: Annotated[StrictInt, Field(ge=2, le=128)] = 32
    events_per_channel: Annotated[StrictInt, Field(ge=2, le=256)] = 64

    @field_validator('concurrency',mode='before')
    @classmethod
    def strict_concurrency(cls,value):
        if type(value)is not int:raise ValueError('Concurrency must be an integer')
        return value

    @model_validator(mode='after')
    def bounds(self):
        if self.backoff_max_seconds < self.backoff_initial_seconds:raise ValueError('Invalid backoff limits')
        if len(self.online_accounts)!=len(set(self.online_accounts)):raise ValueError('Duplicate accounts')
        if type(self.concurrency) is not int:raise ValueError('Concurrency must be an integer')
        return self


class ChannelScope(Model):
    kind: Kind
    account_id: Id | None
    platform: Literal['douyin','xiaohongshu','bilibili','youtube'] | None
    account_version: Positive | None
    application_source: Annotated[StrictStr, Field(max_length=80)] | None
    installed_code_sha256: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')] | None
    component_revision: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{40}$')] | None
    artifact_sha256: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')] | None
    evidence_mode: Literal['contract','live']


class ChannelItem(Model):
    check_type: Code
    source: Source
    result: Result
    reason_code: Code
    value: Annotated[StrictStr, Field(max_length=128)] | StrictInt | StrictBool | None = None


class ChannelDeclaration(Model):
    operation: Literal['check','upload','query','native_schedule']
    supported: StrictBool
    configured: StrictBool
    native_verification: Literal['contract_tested_live_unverified','unavailable']
    evidence_status: Evidence
    evidence_scope: ScopeKind = 'CAPABILITY_DECLARATION'


class InspectionCounts(Model):
    generate: Annotated[StrictInt, Field(ge=0)] = 0
    paid_model: Annotated[StrictInt, Field(ge=0)] = 0
    authorize: Annotated[StrictInt, Field(ge=0)] = 0
    upload: Annotated[StrictInt, Field(ge=0)] = 0
    query: Annotated[StrictInt, Field(ge=0)] = 0
    check: Annotated[StrictInt, Field(ge=0)] = 0
    version_process: Annotated[StrictInt, Field(ge=0)] = 0
    cli_help_process: Annotated[StrictInt, Field(ge=0)] = 0
    wheel_verify_process: Annotated[StrictInt, Field(ge=0)] = 0


class InspectionOutcome(Model):
    result: Result
    reason_code: Code
    evidence_status: Evidence = 'unverified'
    evidence_scope: ScopeKind
    checks: Annotated[list[ChannelItem], Field(max_length=32)]
    declarations: Annotated[list[ChannelDeclaration], Field(max_length=4)] = []
    actual_operations: InspectionCounts


class ChannelReport(InspectionOutcome):
    id: Id
    channel_id: Id
    channel_version: Positive
    check_id: Id
    observed_at: StrictStr
    started_at: StrictStr
    completed_at: StrictStr
    valid_until: StrictStr
    scope: ChannelScope
    freshness: Freshness
    superseded_by_version: Positive | None


class ChannelCheck(Model):
    id: Id
    channel_id: Id
    channel_version: Positive
    state: CheckState
    online: StrictBool
    created_at: StrictStr
    started_at: StrictStr | None
    completed_at: StrictStr | None
    report_id: Id | None
    reason_code: Code


class ChannelCheckResult(Model):
    check: ChannelCheck
    joined: StrictBool


class ChannelCheckRequest(Model):
    expected_version: Positive
    online: StrictBool = False
    confirmed_online: StrictBool = False

    @model_validator(mode='after')
    def consent(self):
        if self.online and not self.confirmed_online:raise ValueError('Confirm the read-only online check')
        if not self.online and self.confirmed_online:raise ValueError('Unexpected online confirmation')
        return self


class ChannelActions(Model):
    local_check: StrictBool
    online_check: StrictBool
    reason_code: Code
    retry_after_seconds: Annotated[StrictInt, Field(ge=0)]


class Channel(Model):
    id: Id
    kind: Kind
    channel_version: Positive
    scope: ChannelScope
    last_inventory_at: StrictStr | None
    latest_report: ChannelReport | None
    freshness: Freshness
    active_check: ChannelCheck | None
    actions: ChannelActions


class ChannelList(Model):
    items: list[Channel]
    next_cursor: StrictStr | None
    policy: InspectionSettings
