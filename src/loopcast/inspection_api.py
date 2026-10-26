"""Explicit read-only inspection requests; GET never initiates a probe."""
import asyncio
from typing import Annotated
from fastapi import Header, Query, Request
from .studio_models import Id
from .inspection_models import ChannelList, Channel, ChannelReport, ChannelCheck, ChannelCheckRequest, ChannelCheckResult


def register_inspection_routes(app, errors):
    @app.get('/v1/operations/channels',response_model=ChannelList,responses=errors)
    async def channels(request:Request,cursor:Annotated[str|None,Query(max_length=512)]=None,limit:Annotated[int,Query(ge=1,le=100)]=30):
        return await asyncio.to_thread(request.app.state.runtime.inspections.list,cursor,limit)

    @app.get('/v1/operations/channels/{channel_id}',response_model=Channel,responses=errors)
    async def channel(channel_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.inspections.channel,channel_id)

    @app.post('/v1/operations/channels/{channel_id}/checks',response_model=ChannelCheckResult,status_code=202,responses=errors)
    async def check(channel_id:Id,body:ChannelCheckRequest,request:Request,idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
        return await asyncio.to_thread(request.app.state.runtime.inspections.start,channel_id,body,idempotency_key)

    @app.get('/v1/operations/channel-checks/{check_id}',response_model=ChannelCheck,responses=errors)
    async def check_state(check_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.store.inspection_check,check_id)

    @app.get('/v1/operations/channel-reports/{report_id}',response_model=ChannelReport,responses=errors)
    async def report(report_id:Id,request:Request):
        return await asyncio.to_thread(request.app.state.runtime.inspections.report,report_id)
