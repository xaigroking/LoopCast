"""HTTP adapters for the creation workspace; no business database ownership."""
import asyncio
from typing import Annotated
from fastapi import Header, Request
from .studio_models import ProjectCreate, Project, ProjectList, DraftUpdate, VersionCommand, GenerateRequest, Id

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
        result = await asyncio.to_thread(runtime.store.studio_generate, project_id, revision_id, body.stage, body.expected_version, idempotency_key, request.state.trace)
        runtime.worker.wake.set()
        return result
