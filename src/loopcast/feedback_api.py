"""Manual observation adapters; no platform scraping or automatic side effects."""
import asyncio
from urllib.parse import quote, unquote
from typing import Annotated
from fastapi import Header, Request
from fastapi.responses import Response
from .domain import Problem
from .feedback_models import WorkBindingCreate, WorkBinding, MetricsImport, ImportReport, FeedbackWorkspace, DecisionCreate, DecisionConfirm, EditorialDecision
from .studio_models import Id, VersionCommand

Key=Annotated[str|None,Header(alias='Idempotency-Key')]


def register_feedback_routes(app, errors):
    @app.get('/v1/feedback',response_model=FeedbackWorkspace,responses=errors)
    async def workspace(request:Request):
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_workspace)

    @app.get('/v1/feedback/schema',responses=errors)
    async def schema():return MetricsImport.model_json_schema()

    @app.get('/v1/feedback/template',responses=errors)
    async def template():
        return {'format':'loopcast.metrics.v1','source':'Describe the manual export and metric scope','evidence_mode':'live','rows':[]}

    @app.post('/v1/feedback/works',response_model=WorkBinding,responses=errors)
    async def bind(body:WorkBindingCreate,request:Request,idempotency_key:Key=None):
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_bind,body.model_dump(exclude={'confirmed'}),idempotency_key)

    @app.post('/v1/feedback/imports',response_model=ImportReport,responses=errors)
    async def import_metrics(body:MetricsImport,request:Request,idempotency_key:Key=None,x_file_name:Annotated[str,Header()]='observations.json'):
        name=unquote(x_file_name).replace('\\','/').split('/')[-1]
        if not name or len(name)>120:raise Problem('INVALID_INPUT','Use a bounded filename.',400)
        raw=await request.body()
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_import,raw,name,idempotency_key)

    @app.get('/v1/feedback/imports/{import_id}/original',responses=errors)
    async def original(import_id:Id,request:Request):
        value=await asyncio.to_thread(request.app.state.runtime.store.feedback_raw,import_id)
        return Response(value['data'],media_type='application/json',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(value['filename'],safe=''),'Cache-Control':'private, no-store'})

    @app.post('/v1/feedback/imports/{import_id}/reprocess',response_model=ImportReport,responses=errors)
    async def reprocess(import_id:Id,body:VersionCommand,request:Request,idempotency_key:Key=None):
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_reprocess,import_id,body.expected_version,idempotency_key)

    @app.get('/v1/feedback/decisions',response_model=list[EditorialDecision],responses=errors)
    async def decisions(request:Request):return await asyncio.to_thread(request.app.state.runtime.store.feedback_decisions)

    @app.post('/v1/feedback/decisions',response_model=EditorialDecision,responses=errors)
    async def propose(body:DecisionCreate,request:Request,idempotency_key:Key=None):
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_propose,body.model_dump(),idempotency_key)

    @app.post('/v1/feedback/decisions/{decision_id}/confirm',response_model=EditorialDecision,responses=errors)
    async def confirm(decision_id:Id,body:DecisionConfirm,request:Request,idempotency_key:Key=None):
        return await asyncio.to_thread(request.app.state.runtime.store.feedback_confirm,decision_id,body.model_dump(exclude={'confirmed'}),idempotency_key)
