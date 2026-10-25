"""HTTP adapters for the creation workspace; no business database ownership."""
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import unquote
from typing import Annotated, Literal
from fastapi import Header, Request
from fastapi.responses import FileResponse, Response
from .domain import Problem
from .media import LIMIT
from .studio_models import (ProjectCreate, Project, ProjectList, DraftUpdate, VersionCommand, GenerateRequest, Id,
    Asset, AssetList, StudioCapabilities, Account, AccountList, AccountUpdate, AuthorizationCreate, AuthorizationCancel, Authorization, Interaction,
    PreflightRequest, Preflight, ReleaseCreate, ReleaseBatch, ReleaseList)

Key = Annotated[str | None, Header(alias='Idempotency-Key')]


def register_studio_routes(app, error_responses):
    @app.get('/v1/studio/projects', response_model=ProjectList, responses=error_responses)
    async def list_projects(request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_projects)

    @app.post('/v1/studio/projects', response_model=Project, responses=error_responses)
    async def create_project(body: ProjectCreate, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_create, body.title, idempotency_key)

    @app.get('/v1/studio/projects/{project_id}', response_model=Project, responses=error_responses)
    async def get_project(project_id: Id, request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_project, project_id)

    @app.put('/v1/studio/projects/{project_id}', response_model=Project, responses=error_responses)
    async def save_project(project_id: Id, body: DraftUpdate, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_save, project_id, body.draft.model_dump(), body.expected_version, idempotency_key)

    @app.post('/v1/studio/projects/{project_id}/revisions', response_model=Project, responses=error_responses)
    async def freeze_project(project_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_revision, project_id, body.expected_version, idempotency_key)

    @app.post('/v1/studio/projects/{project_id}/revisions/{revision_id}/generate', response_model=Project, responses=error_responses)
    async def generate_revision(project_id: Id, revision_id: Id, body: GenerateRequest, request: Request, idempotency_key: Key = None):
        runtime = request.app.state.runtime
        result = await asyncio.to_thread(runtime.studio.generate, project_id, revision_id, body.stage, body.expected_version, idempotency_key, request.state.trace)
        runtime.worker.wake.set()
        return result

    @app.get('/v1/studio/capabilities', response_model=StudioCapabilities, responses=error_responses)
    async def studio_capabilities(request: Request):
        return request.app.state.runtime.studio.capabilities()

    @app.get('/v1/studio/assets', response_model=AssetList, responses=error_responses)
    async def list_assets(request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_assets)

    @app.post('/v1/studio/assets/import', response_model=Asset, responses=error_responses,
              openapi_extra={'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def import_asset(request: Request, kind: Literal['video', 'audio', 'image'], parent_id: Id | None = None,
                           idempotency_key: Key = None, x_file_name: Annotated[str, Header()] = 'media', x_source: Annotated[str, Header()] = ''):
        runtime = request.app.state.runtime
        if not runtime.media.settings: raise Problem('CAPABILITY_UNAVAILABLE', 'Media tools are not configured.', 422)
        if not idempotency_key: raise Problem('IDEMPOTENCY_REQUIRED', 'Supply an Idempotency-Key.', 400)
        source = unquote(x_source)
        name = unquote(x_file_name).replace('\\', '/').split('/')[-1]
        if not source.strip() or len(source) > 500 or not name or len(name) > 120:
            raise Problem('INVALID_INPUT', 'Provide an asset name and source.', 400)
        with tempfile.NamedTemporaryFile(dir=runtime.media.root, suffix='.upload', delete=False) as stream:
            path = Path(stream.name)
            try:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > LIMIT: raise Problem('INVALID_MEDIA', 'Asset exceeds 128 MiB.', 400)
                    await asyncio.to_thread(stream.write, chunk)
                await asyncio.to_thread(stream.flush)
                return await asyncio.to_thread(runtime.media.register, path, kind, name, source, idempotency_key, parent_id=parent_id)
            finally:
                path.unlink(missing_ok=True)

    @app.get('/v1/studio/assets/{asset_id}', response_model=Asset, responses=error_responses)
    async def get_asset(asset_id: Id, request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_asset, asset_id)

    @app.get('/v1/studio/assets/{asset_id}/content', responses={**error_responses, 200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def asset_content(asset_id: Id, request: Request):
        runtime = request.app.state.runtime
        asset = await asyncio.to_thread(runtime.store.studio_asset, asset_id)
        path = await asyncio.to_thread(runtime.media.path, asset_id)
        return FileResponse(path, media_type=asset['media_type'], filename=path.name,
                            content_disposition_type='attachment' if asset['kind'] in {'script', 'subtitle'} else 'inline', headers={'Cache-Control': 'private, no-store'})

    @app.get('/v1/studio/accounts', response_model=AccountList, responses=error_responses)
    async def list_accounts(request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_accounts)

    @app.put('/v1/studio/accounts/{account_id}', response_model=Account, responses=error_responses)
    async def update_account(account_id: Id, body: AccountUpdate, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_account_update, account_id, body.label, body.tags, body.expected_version, idempotency_key)

    @app.post('/v1/studio/accounts/{account_id}/disconnect', response_model=Account, responses=error_responses)
    async def disconnect_account(account_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.studio.disconnect, account_id, body.expected_version, idempotency_key)

    @app.post('/v1/studio/accounts/{account_id}/check', response_model=Account, responses=error_responses)
    async def check_account(account_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_check_account, account_id, body.expected_version, idempotency_key, request.state.trace)

    @app.get('/v1/studio/authorizations', response_model=list[Authorization], responses=error_responses)
    async def list_authorizations(request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_authorizations, request.state.owner_hash)

    @app.post('/v1/studio/authorizations', response_model=Authorization, responses=error_responses)
    async def start_authorization(body: AuthorizationCreate, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.studio.authorize, body.platform, body.account_id, body.method, request.state.owner_hash, idempotency_key, request.state.trace)

    @app.get('/v1/studio/authorizations/{session_id}', response_model=Authorization, responses=error_responses)
    async def get_authorization(session_id: Id, request: Request):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_authorization, session_id, request.state.owner_hash)

    @app.post('/v1/studio/authorizations/{session_id}/cancel', response_model=Authorization, responses=error_responses)
    async def cancel_authorization(session_id: Id, request: Request, body: AuthorizationCancel, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.store.studio_cancel_authorization, session_id, request.state.owner_hash, idempotency_key)

    @app.post('/v1/studio/authorizations/{session_id}/controls', response_model=Authorization, responses=error_responses)
    async def authorization_controls(session_id: Id, body: Interaction, request: Request, idempotency_key: Key = None):
        return await asyncio.to_thread(request.app.state.runtime.studio.interact, session_id, request.state.owner_hash, idempotency_key, body.model_dump(exclude_none=True))

    @app.get('/v1/studio/authorizations/{session_id}/frame', responses={**error_responses, 200: {'content': {'image/png': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def authorization_frame(session_id: Id, request: Request):
        image = await asyncio.to_thread(request.app.state.runtime.studio.frame, session_id, request.state.owner_hash)
        return Response(image, media_type='image/png', headers={'Cache-Control': 'private, no-store'})

    @app.post('/v1/studio/releases/preflight', response_model=Preflight, responses=error_responses)
    async def preflight_release(body: PreflightRequest, request: Request):
        return await asyncio.to_thread(request.app.state.runtime.studio.preflight, body.model_dump())

    @app.post('/v1/studio/releases', response_model=ReleaseBatch, responses=error_responses)
    async def create_release(body: ReleaseCreate, request: Request, idempotency_key: Key = None):
        value = body.model_dump(exclude={'preflight_digest', 'confirmed'})
        return await asyncio.to_thread(request.app.state.runtime.studio.create_release, value, body.preflight_digest, idempotency_key)

    @app.get('/v1/studio/releases', response_model=ReleaseList, responses=error_responses)
    async def list_releases(request: Request):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.studio_releases, runtime.studio.query_platforms)

    @app.get('/v1/studio/releases/{batch_id}', response_model=ReleaseBatch, responses=error_responses)
    async def get_release(batch_id: Id, request: Request):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.studio_release, batch_id, runtime.studio.query_platforms)

    @app.post('/v1/studio/releases/{batch_id}/targets/{target_id}/dispatch', response_model=ReleaseBatch, responses=error_responses)
    async def dispatch_target(batch_id: Id, target_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.studio_dispatch, batch_id, target_id, body.expected_version, idempotency_key,
            runtime.studio.platform_map, runtime.social.sending_enabled, request.state.trace, runtime.social.timeout)

    @app.post('/v1/studio/releases/{batch_id}/targets/{target_id}/verify', response_model=ReleaseBatch, responses=error_responses)
    async def verify_target(batch_id: Id, target_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.studio_verify, batch_id, target_id, body.expected_version, idempotency_key, runtime.studio.query_platforms, request.state.trace)

    @app.post('/v1/studio/releases/{batch_id}/targets/{target_id}/cancel', response_model=ReleaseBatch, responses=error_responses)
    async def cancel_target(batch_id: Id, target_id: Id, body: VersionCommand, request: Request, idempotency_key: Key = None):
        runtime = request.app.state.runtime
        return await asyncio.to_thread(runtime.store.studio_cancel_release, batch_id, target_id, body.expected_version, idempotency_key, runtime.studio.query_platforms)
