"""Published contracts for manual observations and reproducible editorial decisions."""
from typing import Annotated, Literal
from pydantic import Field
from .studio_models import Model, Id, Platform, Number, VersionCommand

Timestamp = Annotated[str, Field(min_length=1, max_length=50)]
Subject = Annotated[str, Field(pattern=r'^[A-Za-z0-9_:.@-]{1,128}$')]
WorkId = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')]
Counter = Annotated[Number, Field(ge=0, le=9007199254740991)]


class Metrics(Model):
    views: Counter | None = None
    likes: Counter | None = None
    comments: Counter | None = None
    shares: Counter | None = None
    watch_seconds: Counter | None = None


class WorkBindingCreate(Model):
    account_id: Id
    asset_id: Id
    work_id: WorkId
    release_target_id: Id | None = None
    published_at: Timestamp
    source: Annotated[str, Field(min_length=1, max_length=500)]
    confirmed: Literal[True]


class WorkBinding(Model):
    id: Id
    account_id: Id
    asset_id: Id
    platform: Platform
    stable_subject: str
    work_id: str
    release_target_id: Id | None
    published_at: str
    source: str
    evidence_mode: Literal['live','contract']
    evidence_kind: Literal['PLATFORM_RECEIPT','MANUAL_ASSOCIATION']
    created_at: str


class MetricRow(Model):
    platform: Platform
    stable_subject: Subject
    work_id: WorkId
    asset_id: Id
    observed_at: Timestamp
    aggregation: Literal['cumulative','delta']
    definition: Literal['platform_reported_v1']
    window_start: Timestamp | None = None
    window_end: Timestamp | None = None
    metrics: Metrics


class MetricsImport(Model):
    format: Literal['loopcast.metrics.v1']
    source: Annotated[str, Field(min_length=1,max_length=500)]
    evidence_mode: Literal['live','contract']
    rows: Annotated[list[MetricRow], Field(min_length=1,max_length=500)]


class ImportRowResult(Model):
    row: int
    state: Literal['ACCEPTED','DUPLICATE','QUARANTINED']
    code: str
    snapshot_id: Id | None


class ImportReport(Model):
    id: Id
    filename: str
    sha256: str
    source: str
    evidence_mode: Literal['live','contract']
    version: int
    created_at: str
    rows: list[ImportRowResult]
    resolution_history: list[dict[str, str | int]]


class MetricSnapshot(Model):
    id: Id
    import_id: Id
    work_binding_id: Id
    platform: Platform
    stable_subject: str
    work_id: str
    asset_id: Id
    observed_at: str
    original_observed_at: str
    aggregation: str
    definition: str
    window_start: str | None
    window_end: str | None
    metrics: Metrics
    evidence_mode: Literal['live','contract']
    sha256: str
    created_at: str


class FeedbackWorkspace(Model):
    works: list[WorkBinding]
    imports: list[ImportReport]
    snapshots: list[MetricSnapshot]


class DecisionCreate(Model):
    snapshot_ids: Annotated[list[Id], Field(max_length=50)]
    topic: Annotated[str,Field(min_length=1,max_length=120)]
    brief: Annotated[str,Field(max_length=2000)] = ''
    goal: Literal['views'] = 'views'
    minimum_age_hours: Annotated[int,Field(ge=0,le=8760)] = 24
    minimum_views: Annotated[int,Field(ge=0,le=9007199254740991)] = 100


class DecisionConfirm(VersionCommand):
    topic: Annotated[str,Field(min_length=1,max_length=120)]
    brief: Annotated[str,Field(max_length=2000)]
    project_id: Id | None = None
    expected_project_version: Annotated[int,Field(ge=1)] | None = None
    confirmed: Literal[True]


class DecisionRevision(Model):
    revision_id: Id
    project_id: Id
    project_version: int
    created_at: str


class EditorialDecision(Model):
    id: Id
    version: int
    state: Literal['PROPOSED','CONFIRMED']
    strategy_version: str
    input: DecisionCreate
    snapshot_digests: dict[str,str]
    selected_snapshot_id: Id | None
    reason: str
    excluded: dict[str,str]
    evidence_mode: Literal['live','contract'] | None
    created_at: str
    revisions: list[DecisionRevision]
