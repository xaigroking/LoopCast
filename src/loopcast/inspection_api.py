"""Explicit read-only inspection requests; GET never initiates a probe."""
import asyncio
from typing import Annotated
from fastapi import Header, Query, Request
from .studio_models import Id
from .domain import Problem
from fastapi.responses import JSONResponse
from .inspection_models import ChannelList, Channel, ChannelReport, ChannelCheck, ChannelCheckRequest, ChannelCheckResult, ChannelReportList, ChannelEventList, InspectionErrorEnvelope


def register_inspection_routes(app, errors):
    @app.get('/v1/operations/channels',response_model=ChannelList,responses=errors)
    async def channels(request:Request,cursor:Annotated[str|None,Query(max_length=512)]=None,limit:Annotated[int,Query(ge=1,le=100)]=30):
        return await asyncio.to_thread(request.app.state.runtime.inspections.list,cursor,limit)

    @app.get('/v1/operations/channels/{channel_id}',response_model=Channel,responses=errors)
    async def channel(channel_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.inspections.channel,channel_id)

    @app.post('/v1/operations/channels/{channel_id}/checks',response_model=ChannelCheckResult,status_code=202,responses=errors|{409:{'model':InspectionErrorEnvelope},429:{'model':InspectionErrorEnvelope}})
    async def check(channel_id:Id,body:ChannelCheckRequest,request:Request,idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
        service=request.app.state.runtime.inspections
        try:return await asyncio.to_thread(service.start,channel_id,body,idempotency_key)
        except Problem as error:
            if error.status not in {409,429}:raise
            current=await asyncio.to_thread(service.channel,channel_id)
            retry=current['actions']['retry_after_seconds'] if error.code=='CHECK_COOLDOWN' else 1 if error.code=='QUEUE_FULL' else None
            value=InspectionErrorEnvelope(error={'code':error.code,'message':str(error),'request_id':request.state.request_id,'current_version':current['channel_version'],'retry_after_seconds':retry})
            return JSONResponse(value.model_dump(),status_code=error.status,headers={'Retry-After':str(retry)} if retry else {})

    @app.get('/v1/operations/channel-checks/{check_id}',response_model=ChannelCheck,responses=errors)
    async def check_state(check_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.store.inspection_check,check_id)

    @app.get('/v1/operations/channel-reports/{report_id}',response_model=ChannelReport,responses=errors)
    async def report(report_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.inspections.report,report_id)

    @app.get('/v1/operations/channels/{channel_id}/reports',response_model=ChannelReportList,responses=errors)
    async def reports(channel_id:Id,request:Request,cursor:Annotated[str|None,Query(max_length=512)]=None,limit:Annotated[int,Query(ge=1,le=100)]=30):
        service=request.app.state.runtime.inspections
        descriptor=await asyncio.to_thread(service.find,channel_id)
        return await asyncio.to_thread(service.store.inspection_reports,descriptor,cursor,limit)

    @app.get('/v1/operations/channel-events',response_model=ChannelEventList,responses=errors)
    async def events(request:Request,channel_id:Id|None=None,cursor:Annotated[str|None,Query(max_length=512)]=None,limit:Annotated[int,Query(ge=1,le=100)]=30):
        service=request.app.state.runtime.inspections
        if channel_id:await asyncio.to_thread(service.find,channel_id)
        return await asyncio.to_thread(service.store.inspection_events,channel_id,cursor,limit)
