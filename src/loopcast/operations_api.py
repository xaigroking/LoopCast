"""Only allowlisted read operations are exposed to ordinary browser requests."""
import asyncio
from fastapi import Request
from .operations_models import OperationsStatus, ReconciliationReport, OperationsSnapshot
from .operations_snapshot import snapshot


def register_operations_routes(app, errors):
    @app.get('/v1/operations/status',response_model=OperationsStatus,responses=errors)
    async def status(request:Request):
        runtime = request.app.state.runtime
        value = await asyncio.to_thread(runtime.store.operations_status)
        configured = any(p['configured'] and p['video'] for p in runtime.social.capabilities())
        return value | {'service_ready':runtime.ready(),'configured_sending_enabled':runtime.social.sending_enabled,
                        'effective_sending_enabled':configured and runtime.social.sending_enabled and value['upload_allowed_by_recovery'],
                        'evidence_mode':runtime.social.mode}

    @app.get('/v1/operations/reconciliation',response_model=ReconciliationReport,responses=errors)
    async def reconciliation(request:Request):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.operations_reconciliation,runtime.studio.query_platforms)

    @app.get('/v1/operations/snapshot',response_model=OperationsSnapshot,responses=errors)
    async def runtime_snapshot(request:Request):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(snapshot,runtime.directory,runtime.media_settings,runtime.generator_settings,runtime.ready())
