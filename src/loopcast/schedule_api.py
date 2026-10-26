"""Strict finite local-publication management; the existing owner executes plans."""
import asyncio
from typing import Annotated
from fastapi import Header, Query, Request
from .domain import Problem
from .schedule_models import (Id, SchedulePreviewRequest, SchedulePreview, ScheduleCreateRequest, ScheduleUpdateRequest,
    ScheduleCommand, ScheduleResume, ScheduleRecord, ScheduleList, ScheduleOccurrenceList, ScheduleCapability)

Key=Annotated[str|None,Header(alias='Idempotency-Key')]
Limit=Annotated[int,Query(ge=1,le=100)]
Cursor=Annotated[str|None,Query(max_length=512)]


def register_schedule_routes(app, errors):
    @app.get('/v1/studio/schedules/capabilities',response_model=ScheduleCapability,responses=errors)
    async def capabilities(request:Request):
        runtime=request.app.state.runtime;store=runtime.store
        status=await asyncio.to_thread(store.operations_status)
        with store.transaction() as db:high=db.execute('SELECT high_water FROM scheduling_state WHERE id=1').fetchone()[0]
        clock_ok=store.clock.now()>=high-5
        config=store.schedule_settings
        return {'management':True,'enabled':config.enabled,'allow_live':config.allow_live,
                'effective_sending_enabled':any(p['configured'] and p['video'] for p in runtime.social.capabilities()) and config.enabled and runtime.social.sending_enabled and status['upload_allowed_by_recovery'] and clock_ok and (runtime.social.mode=='contract' or config.allow_live),
                'native_scheduling':False,'evidence_mode':runtime.social.mode,'time_rule_version':'local-daily-v1',
                'fold_policy':'EARLIER','gap_policy':'SKIP','clock_state':'STABLE' if clock_ok else 'CLOCK_BACKWARD','limits':config.model_dump()}

    @app.post('/v1/studio/schedules/preview',response_model=SchedulePreview,responses=errors)
    async def preview(body:SchedulePreviewRequest,request:Request):
        runtime=request.app.state.runtime
        return await asyncio.to_thread(runtime.store.schedule_preview,body.spec.model_dump(),runtime.studio.platform_map,runtime.social.mode,
                                       body.schedule_id,body.expected_version,body.cursor)

    @app.get('/v1/studio/schedules',response_model=ScheduleList,responses=errors)
    async def plans(request:Request,cursor:Cursor=None,limit:Limit=30):
        return await asyncio.to_thread(request.app.state.runtime.store.schedule_list,cursor,limit)

    @app.post('/v1/studio/schedules',response_model=ScheduleRecord,responses=errors)
    async def create(body:ScheduleCreateRequest,request:Request,idempotency_key:Key=None):
        runtime=request.app.state.runtime
        replay=await asyncio.to_thread(runtime.store.schedule_replay_create,body.spec.model_dump(),body.preview_digest,idempotency_key)
        if replay is not None:return replay
        # Verify registered bytes without starting a generator or adapter call.
        for group in body.spec.items:
            release=await asyncio.to_thread(runtime.store.studio_release,group.batch_id)
            await asyncio.to_thread(runtime.media.path,release['asset_id'])
        return await asyncio.to_thread(runtime.store.schedule_create,body.spec.model_dump(),body.preview_digest,idempotency_key,runtime.studio.platform_map,runtime.social.mode)

    @app.get('/v1/studio/schedules/{schedule_id}',response_model=ScheduleRecord,responses=errors)
    async def get(schedule_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.store.schedule_get,schedule_id)

    @app.put('/v1/studio/schedules/{schedule_id}',response_model=ScheduleRecord,responses=errors)
    async def update(schedule_id:Id,body:ScheduleUpdateRequest,request:Request,idempotency_key:Key=None):
        runtime=request.app.state.runtime
        return await asyncio.to_thread(runtime.store.schedule_update,schedule_id,body.spec.model_dump(),body.expected_version,body.preview_digest,
                                       body.effective_from_local_date,idempotency_key,runtime.studio.platform_map,runtime.social.mode)

    @app.post('/v1/studio/schedules/{schedule_id}/pause',response_model=ScheduleRecord,responses=errors)
    async def pause(schedule_id:Id,body:ScheduleCommand,request:Request,idempotency_key:Key=None):
        runtime=request.app.state.runtime
        return await asyncio.to_thread(runtime.store.schedule_control,schedule_id,'pause',body.expected_version,idempotency_key,runtime.studio.platform_map,runtime.social.mode)

    @app.post('/v1/studio/schedules/{schedule_id}/resume',response_model=ScheduleRecord,responses=errors)
    async def resume(schedule_id:Id,body:ScheduleResume,request:Request,idempotency_key:Key=None):
        runtime=request.app.state.runtime
        return await asyncio.to_thread(runtime.store.schedule_control,schedule_id,'resume',body.expected_version,idempotency_key,runtime.studio.platform_map,runtime.social.mode,body.confirmation_digest,sending_enabled=runtime.social.sending_enabled)

    @app.post('/v1/studio/schedules/{schedule_id}/cancel',response_model=ScheduleRecord,responses=errors)
    async def cancel(schedule_id:Id,body:ScheduleCommand,request:Request,idempotency_key:Key=None):
        runtime=request.app.state.runtime
        return await asyncio.to_thread(runtime.store.schedule_control,schedule_id,'cancel',body.expected_version,idempotency_key,runtime.studio.platform_map,runtime.social.mode)

    @app.get('/v1/studio/schedules/{schedule_id}/occurrences',response_model=ScheduleOccurrenceList,responses=errors)
    async def occurrences(schedule_id:Id,request:Request,cursor:Cursor=None,limit:Limit=30):
        return await asyncio.to_thread(request.app.state.runtime.store.schedule_occurrences,schedule_id,cursor,limit)
