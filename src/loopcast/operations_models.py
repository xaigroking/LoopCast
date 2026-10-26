"""Read-only operations contracts. No backup paths or manual release commands."""
from typing import Literal
from .studio_models import Model, Id


class RestoreGuard(Model):
    id: Literal[1]
    restore_id: Id
    version: int
    state: Literal['ACTIVE']
    reason: Literal['EXTERNAL_HISTORY_UNRECONCILED']


class RestoreEvent(Model):
    id: Id
    backup_id: Id
    manifest_sha256: str
    backup_created_at: str
    restored_at: str
    source_schema: int
    execution_epoch: Id
    scope: Literal['ALL_STUDIO_UPLOADS']


class BackupAudit(Model):
    id: Id
    created_at: str
    manifest_sha256: str
    state: Literal['VERIFIED_PRIVATE_BACKUP']


class OperationsStatus(Model):
    restore_guard: RestoreGuard | None
    restore: RestoreEvent | None
    backup: BackupAudit | None
    upload_allowed_by_recovery: bool
    release_supported: bool
    service_ready: bool
    configured_sending_enabled: bool
    effective_sending_enabled: bool
    evidence_mode: Literal['live','contract']


class ReconciliationReceipt(Model):
    id: Id
    source: str
    outcome: str
    work_id: str | None
    evidence_code: str


class ReconciliationTarget(Model):
    target_id: Id
    account_id: Id
    platform: str
    stable_subject: str
    evidence_mode: Literal['live','contract']
    state: str
    known_publication: Literal['FOUND','INCONCLUSIVE']
    query: Literal['available','unavailable']
    receipts: list[ReconciliationReceipt]


class ReconciliationReport(Model):
    format: Literal['loopcast.reconciliation.v1']
    guard: RestoreGuard | None
    restore: RestoreEvent | None
    scope: Literal['ALL_STUDIO_UPLOADS']
    coverage: Literal['INCONCLUSIVE','NOT_RESTORED']
    release_eligible: Literal[False]
    reason: Literal['NO_COMPLETE_EXTERNAL_COVERAGE_VERIFIER']
    targets: list[ReconciliationTarget]
    unknown_external_works: Literal['UNBOUNDED_AFTER_BACKUP','NOT_ASSESSED']


class Observation(Model):
    value: str | int | bool | dict[str,int] | None
    status: Literal['verified','unverified','unavailable']
    source: str


class OperationsSnapshot(Model):
    format: Literal['loopcast.snapshot.v1']
    observed_at: str
    application: dict[str,Observation]
    runtime: dict[str,Observation]
    database: dict[str,Observation]
    media: dict[str,Observation]
    generator: dict[str,Observation]
    recovery: dict[str,Observation]
