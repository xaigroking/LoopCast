"""Trusted scheduling limits; no automatic sending without explicit configuration."""
from pydantic import Field, StrictBool, StrictInt
from typing import Annotated, Literal
from .studio_models import Model


class ScheduleSettings(Model):
    format: Literal['loopcast.scheduling.v1'] = 'loopcast.scheduling.v1'
    enabled: StrictBool = False
    allow_live: StrictBool = False
    max_active_plans: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    max_slots_per_plan: Annotated[StrictInt, Field(ge=1, le=366)] = 366
    max_pending_targets: Annotated[StrictInt, Field(ge=1, le=4096)] = 256
    account_pending_targets: Annotated[StrictInt, Field(ge=1, le=4096)] = 64
    plan_pending_targets: Annotated[StrictInt, Field(ge=1, le=4096)] = 32
    plan_attempts_per_utc_day: Annotated[StrictInt, Field(ge=1, le=100)] = 10
    account_attempts_per_utc_day: Annotated[StrictInt, Field(ge=1, le=100)] = 10
    max_catchup_age_seconds: Annotated[StrictInt, Field(ge=60, le=86400)] = 3600
    max_catchup_slots: Annotated[StrictInt, Field(ge=1, le=3)] = 1


class SystemClock:
    def now(self):
        import time
        return time.time()

    def iso(self):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.now(), timezone.utc).isoformat()
