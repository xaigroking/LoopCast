"""Strict finite local-publication schedule contracts."""
from datetime import date
from typing import Annotated, Literal
from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator
from .studio_models import Model, Id, Number

Date = Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
LocalTime = Annotated[StrictStr, Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')]
Hash = Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
Version = Annotated[StrictInt, Field(ge=1)]


class ReleaseGroup(Model):
    batch_id: Id
    target_ids: Annotated[list[Id], Field(min_length=1, max_length=4)]

    @field_validator('target_ids')
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):raise ValueError('Choose each target once')
        return value


class SkipMissed(Model):
    policy: Literal['SKIP']


class CatchupMissed(Model):
    policy: Literal['BOUNDED_CATCHUP']
    max_age_seconds: Annotated[StrictInt, Field(ge=60, le=86400)]
    max_slots: Annotated[StrictInt, Field(ge=1, le=3)]


class ScheduleQuota(Model):
    attempts_per_utc_day: Annotated[StrictInt, Field(ge=1, le=100)]
    pending_targets: Annotated[StrictInt, Field(ge=1, le=4096)]


class BaseSpec(Model):
    timezone: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    local_time: LocalTime
    items: Annotated[list[ReleaseGroup], Field(min_length=1, max_length=366)]
    missed: Annotated[SkipMissed | CatchupMissed, Field(discriminator='policy')]
    quota: ScheduleQuota

    @model_validator(mode='after')
    def unique_targets(self):
        targets = [t for group in self.items for t in group.target_ids]
        if len(targets) != len(set(targets)):raise ValueError('A target may appear in only one slot')
        return self


class OnceSpec(BaseSpec):
    kind: Literal['ONCE']
    local_date: Date
    items: Annotated[list[ReleaseGroup], Field(min_length=1, max_length=1)]

    @field_validator('local_date')
    @classmethod
    def calendar(cls, value):
        date.fromisoformat(value)
        return value


class DailySpec(BaseSpec):
    kind: Literal['DAILY_LOCAL']
    start_local_date: Date

    @field_validator('start_local_date')
    @classmethod
    def calendar(cls, value):
        date.fromisoformat(value)
        return value


Spec = Annotated[OnceSpec | DailySpec, Field(discriminator='kind')]


class SchedulePreviewRequest(Model):
    spec: Spec
    schedule_id: Id | None = None
    expected_version: Version | None = None
    cursor: StrictStr | None = None

    @model_validator(mode='after')
    def paired(self):
        if (self.schedule_id is None) != (self.expected_version is None):raise ValueError('Supply both identity and version')
        return self


class ScheduleCreateRequest(Model):
    spec: Spec
    preview_digest: Hash
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def strict_confirmation(cls, value):
        if value is not True:raise ValueError('Explicit confirmation is required')
        return value


class ScheduleUpdateRequest(Model):
    spec: Spec
    preview_digest: Hash
    expected_version: Version
    effective_from_local_date: Date | None


class ScheduleCommand(Model):
    expected_version: Version


class ScheduleResume(ScheduleCommand):
    confirmation_digest: Hash
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def strict_confirmation(cls, value):
        if value is not True:raise ValueError('Explicit confirmation is required')
        return value


class ScheduleBlocker(Model):
    target_id: Id
    reasons: list[StrictStr]


class ScheduleSlot(Model):
    slot_key: StrictStr
    position: StrictInt
    nominal_local: StrictStr
    timezone: StrictStr
    tzif_sha256: Hash
    selected_fold: Literal[0,1] | None
    scheduled_at_utc: StrictStr | None
    reason: StrictStr
    group: ReleaseGroup


class SchedulePreview(Model):
    blockers: list[ScheduleBlocker]
    time_rule_version: Literal['local-daily-v1']
    fold_policy: Literal['EARLIER']
    gap_policy: Literal['SKIP']
    tzif_sha256: Hash
    tzdb_version: StrictStr | None
    preview_digest: Hash
    confirmation_digest: Hash
    expires_at: StrictStr
    total: StrictInt
    items: list[ScheduleSlot]
    next_cursor: StrictStr | None


class ScheduleInFlight(Model):
    target_id: Id
    attempt_id: Id
    state: StrictStr


class ScheduleRecord(Model):
    id: Id
    control_version: Version
    spec_version: Version
    control_state: Literal['ACTIVE','PAUSED','CANCELLED']
    display_state: StrictStr
    pause_reason: StrictStr | None
    confirmed_digest: Hash | None
    confirmed_at: StrictStr | None
    created_at: StrictStr
    spec: Spec
    tzif_sha256: Hash
    counts: dict[str,int]
    slot_count: StrictInt
    classified_count: StrictInt
    in_flight: list[ScheduleInFlight]


class ScheduleList(Model):
    items: list[ScheduleRecord]
    next_cursor: StrictStr | None


class OccurrenceTarget(Model):
    occurrence_id: Id
    target_id: Id
    batch_id: Id
    state: StrictStr
    reason: StrictStr
    attempt_id: Id | None
    job_id: Id | None


class ScheduleOccurrence(Model):
    id: Id
    schedule_id: Id
    slot_key: StrictStr
    spec_version: Version
    nominal_local: StrictStr
    timezone: StrictStr
    tzif_sha256: Hash
    selected_fold: Literal[0,1] | None
    scheduled_at_utc: StrictStr | None
    eligible_until: Number | None
    state: StrictStr
    reason: StrictStr
    version: Version
    created_at: StrictStr
    classified_at: StrictStr
    targets: list[OccurrenceTarget]


class ScheduleOccurrenceList(Model):
    items: list[ScheduleOccurrence]
    next_cursor: StrictStr | None


class ScheduleCapability(Model):
    management: Literal[True]
    enabled: bool
    allow_live: bool
    effective_sending_enabled: bool
    native_scheduling: Literal[False]
    evidence_mode: Literal['contract','live']
    time_rule_version: Literal['local-daily-v1']
    fold_policy: Literal['EARLIER']
    gap_policy: Literal['SKIP']
    clock_state: Literal['STABLE','CLOCK_BACKWARD']
    limits: 'ScheduleSettings'


from .schedule_config import ScheduleSettings
ScheduleCapability.model_rebuild()
