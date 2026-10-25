"""FastAPI adapter. Domain state and worker ownership live in runtime/storage."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
import ipaddress
import json
import re
import time
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

from .runtime import ApplicationRuntime
from .storage import Problem, uid, TERMINAL
from .diagnostics import new_context, log_event
from .studio_api import register_studio_routes

ID = r"[a-f0-9]{32}"
_RUN = re.compile(r"^/v1/runs/([a-f0-9]{32})(?:/(cancel|retry|events))?$")
_PUBLICATION = re.compile(r"^/v1/runs/([a-f0-9]{32})/publications(?:/([a-f0-9]{32})/(dispatch|verify))?$")
_ARTIFACT = re.compile(r"^/v1/artifacts/([a-f0-9]{32})/content$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FixtureCreate(StrictModel):
    mode: Literal["fixture"] = "fixture"
    title: StrictStr = "Untitled fixture"
    scenario: Literal["success", "fail_once", "fail", "slow"] = "success"
    delay_ms: StrictInt = 150


class MPTCreate(StrictModel):
    mode: Literal["mpt"]
    title: StrictStr


class ExpectedVersion(StrictModel):
    expected_version: StrictInt


class PublicationCreate(StrictModel):
    platform: StrictStr | StrictInt | StrictFloat | StrictBool | None
    account: StrictStr | StrictInt | StrictFloat | StrictBool | None
    expected_version: StrictInt


class PublicationDispatch(StrictModel):
    expected_version: StrictInt
    scenario: Literal["success", "submitted", "lost_response", "not_sent", "slow"] = "success"


class PublicationVerify(StrictModel):
    expected_version: StrictInt


class Rule(StrictModel):
    enabled: bool
    reason_code: str


class RunActions(StrictModel):
    cancel: Rule
    retry: Rule
    add_simulated_target: Rule


class Stage(StrictModel):
    id: str
    stage: str
    state: str
    version: int


class Attempt(StrictModel):
    id: str
    number: int
    state: str
    started_at: str | None
    ended_at: str | None
    error: str | None
    evidence: str | None


class Artifact(StrictModel):
    id: str
    attempt_id: str
    media_type: str
    size: int
    sha256: str


class PublicationAttempt(StrictModel):
    id: str
    state: str
    started_at: str
    ended_at: str | None


class Receipt(StrictModel):
    id: str
    created_at: str
    source: str
    outcome: str
    work_id: str | None
    evidence: str


class PublicationActions(StrictModel):
    dispatch: Rule
    verify: Rule


class Publication(StrictModel):
    id: str
    run_id: str
    content_id: str
    platform: str
    account: str
    state: str
    version: int
    created_at: str
    mode: Literal["simulated"]
    allowed_actions: PublicationActions
    attempts: list[PublicationAttempt]
    receipts: list[Receipt]


class FixtureInput(StrictModel):
    mode: Literal["fixture"]
    title: str
    scenario: str
    delay_ms: int


class GeneratedInput(StrictModel):
    mode: Literal["mpt"]
    title: str
    component_id: str


class ComponentCapabilities(StrictModel):
    generate: bool
    publish: bool
    query: bool
    schedule: bool
    ai_disclosure: bool


class ComponentManifest(StrictModel):
    source: str
    revision: str
    artifact_sha256: str
    contract_major: int
    capabilities: ComponentCapabilities
    verification: Literal["unverified", "contract_tested"]


class Media(StrictModel):
    artifact_id: str
    content_id: str
    parent_artifact_id: str | None
    metadata: str


class RunSnapshot(StrictModel):
    run_id: str
    mode: Literal["fixture", "mpt"]
    input: FixtureInput | GeneratedInput
    input_digest: str
    state: str
    state_version: int
    created_at: str
    updated_at: str
    last_event_id: int
    tasks: list[Stage]
    attempts: list[Attempt]
    artifacts: list[Artifact]
    allowed_actions: RunActions
    publications: list[Publication]
    component: ComponentManifest | None
    content_id: str | None
    media: list[Media]


class RunPage(StrictModel):
    items: list[RunSnapshot]
    next_cursor: int | None


class ErrorDetails(StrictModel):
    code: str
    message: str
    request_id: str
    run_id: str | None
    snapshot: RunSnapshot | None = None


class ErrorEnvelope(StrictModel):
    error: ErrorDetails


class Capabilities(StrictModel):
    mode: Literal["fixture"]
    video_generation: bool
    publishing: bool
    single_user: Literal[True]
    generation_verification: str
    real_platform_sending: bool


ERROR_RESPONSES = {
    status: {"model": ErrorEnvelope, "description": "Stable LoopCast error envelope"}
    for status in (400, 403, 404, 409, 415, 422, 429, 500)
}
def _instrument_response(request, response, started):
    request_id = request.state.request_id
    response.headers["X-Request-ID"] = request_id
    response.headers["traceparent"] = request.state.trace["traceparent"]
    if getattr(request.state, 'new_owner', False):
        response.set_cookie('loopcast_owner', request.state.owner_token, httponly=True, secure=request.state.public_https, samesite='strict', max_age=86400)
    route = request.scope.get("route")
    ids = {key: value for key, value in request.path_params.items() if key in {"run_id", "intent_id", "project_id", "revision_id", "asset_id", "account_id", "session_id", "batch_id", "target_id"} and re.fullmatch(ID, value)}
    log_event("request", "request.completed", request.state.trace,
              method=request.method if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "<other>",
              route=getattr(route, "path", "<unmatched>"),
              status=response.status_code, duration_ms=round((time.monotonic() - started) * 1000, 2), **ids)
    return response


def _error(error: Problem, request_id: str):
    payload = {"code": error.code, "message": str(error), "request_id": request_id, "run_id": error.run_id}
    return JSONResponse(ErrorEnvelope(error=payload).model_dump(exclude_none=True), status_code=error.status)


def _snapshot_response(value, status=200):
    snapshot = RunSnapshot.model_validate(value)
    return JSONResponse(snapshot.model_dump(), status_code=status)


def _event_snapshot(payload):
    value = json.loads(payload)
    # Early fixture history predates generation/publication. Upcast only those
    # additive fields when reading an event; never rewrite the recorded payload
    # or copy today's publication facts into a historical snapshot.
    if isinstance(value, dict) and value.get('mode') == 'fixture':
        for key, default in {'publications': [], 'component': None, 'content_id': None, 'media': []}.items():
            value.setdefault(key, default)
        if isinstance(value.get('allowed_actions'), dict):
            value['allowed_actions'].setdefault('add_simulated_target', {'enabled': False, 'reason_code': 'SIMULATOR_ONLY'})
    return RunSnapshot.model_validate(value)


def create_app(
    data_dir="~/.local/share/loopcast",
    generator_settings=None,
    *,
    public_origin: str | None = None,
    trusted_proxies: tuple[str, ...] = (),
    media_settings=None,
    social_settings=None,
):
    """Create an app factory; resources are acquired only inside ASGI lifespan."""
    proxy_networks = tuple(ipaddress.ip_network(item, strict=False) for item in trusted_proxies)
    if proxy_networks and not public_origin:
        raise ValueError("Trusted proxies require an explicitly configured public origin")
    if public_origin:
        from urllib.parse import urlsplit
        origin = urlsplit(public_origin)
        if origin.scheme not in {"http", "https"} or not origin.netloc or origin.path not in {"", "/"} or origin.query or origin.fragment or origin.username or origin.password:
            raise ValueError("Public origin must be an exact HTTP or HTTPS origin")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = ApplicationRuntime(data_dir, generator_settings, media_settings, social_settings)
        app.state.runtime = runtime
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="LoopCast API", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def controls(request: Request, call_next):
        started = time.monotonic()
        request.state.request_id = uid()
        request.state.trace = new_context(request.headers.get("traceparent"), request.state.request_id)
        owner = request.cookies.get('loopcast_owner', '')
        request.state.new_owner = not bool(re.fullmatch(ID, owner))
        request.state.owner_token = uid() if request.state.new_owner else owner
        request.state.owner_hash = hashlib.sha256(request.state.owner_token.encode()).hexdigest()
        request.state.public_https = bool(public_origin and public_origin.startswith('https://'))
        def reject(problem):
            return _instrument_response(request, _error(problem, request.state.request_id), started)
        method = request.method.upper()
        is_write = method not in {"GET", "HEAD", "OPTIONS"}
        client_host = request.client.host if request.client else ""
        try:
            client_ip = ipaddress.ip_address(client_host.split("%", 1)[0])
        except ValueError:
            client_ip = None
        trusted = client_ip is not None and any(client_ip in network for network in proxy_networks)

        host = request.headers.get("host", "")
        scheme = request.url.scheme
        effective_host = host
        if trusted:
            forwarded_host = request.headers.get("x-forwarded-host", "")
            forwarded_proto = request.headers.get("x-forwarded-proto", "")
            # A single value from a configured proxy only; chains are rejected.
            if "," in forwarded_host or "," in forwarded_proto:
                return reject(Problem("INVALID_HOST", "Use the configured public origin.", 403))
            effective_host = forwarded_host
            scheme = forwarded_proto

        if public_origin:
            from urllib.parse import urlsplit
            expected = urlsplit(public_origin)
            host_ok = effective_host == expected.netloc and scheme == expected.scheme
        else:
            server = request.scope.get("server") or ("", 0)
            allowed_hosts = {f"{server[0]}:{server[1]}", f"localhost:{server[1]}"}
            host_ok = host in allowed_hosts and client_ip is not None and client_ip.is_loopback
        if not host_ok:
            return reject(Problem("INVALID_HOST", "Use the local listening address.", 403))

        if is_write and (request.url.path == "/v1" or request.url.path.startswith("/v1/")):
            uploading = method == 'POST' and request.url.path == '/v1/studio/assets/import'
            body_limit = 128 * 1024 * 1024 if uploading else 1048576 if request.url.path.startswith('/v1/studio/') else 16384
            expected_origin = public_origin if public_origin else f"{scheme}://{effective_host}"
            if request.headers.get("origin") != expected_origin or request.headers.get("sec-fetch-site") == "cross-site":
                return reject(Problem("ORIGIN_REJECTED", "Writes require the exact public Origin.", 403))
            if request.headers.get("content-type", "").split(";", 1)[0].strip() != ('application/octet-stream' if uploading else 'application/json'):
                return reject(Problem("INVALID_INPUT", "Use application/octet-stream." if uploading else "Use application/json.", 415))
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                try:
                    size = int(raw_length)
                except ValueError:
                    size = -1
                if size < 0 or size > body_limit:
                    return reject(Problem("INVALID_INPUT", "Request body exceeds its size limit.", 400))
            if not uploading:
                chunks = []
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > body_limit:
                        return reject(Problem("INVALID_INPUT", "Request body exceeds its size limit.", 400))
                    chunks.append(chunk)
                request._body = b"".join(chunks)

        try:
            response = await call_next(request)
        except Problem as exc:
            payload = {"code": exc.code, "message": str(exc), "request_id": request.state.request_id, "run_id": exc.run_id}
            if exc.run_id and exc.status == 409:
                try:
                    payload["snapshot"] = await asyncio.to_thread(request.app.state.runtime.store.snapshot, exc.run_id)
                except (Problem, AttributeError):
                    pass
            response = JSONResponse(ErrorEnvelope(error=payload).model_dump(exclude_none=True), status_code=exc.status)
        except Exception:
            response = JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "The request could not be completed.", "request_id": request.state.request_id, "run_id": None}}, status_code=500)
        response = _instrument_response(request, response, started)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, _exc: RequestValidationError):
        try:
            raw = json.loads((await request.body()).decode("utf-8"))
        except (ValueError, UnicodeError):
            raw = None
        if request.url.path == '/v1/runs' and isinstance(raw, dict) and raw.get("mode", "fixture") not in ("fixture", "mpt"):
            return _error(Problem("CAPABILITY_UNAVAILABLE", "Only fixture mode is available.", 422), getattr(request.state, "request_id", uid()))
        run_id = request.path_params.get("run_id")
        return _error(Problem("INVALID_INPUT", "The request fields or values are invalid.", 400, run_id if isinstance(run_id, str) else None), getattr(request.state, "request_id", uid()))

    @app.get("/healthz", response_model=dict[str, bool], responses={503: {"model": dict[str, bool]}})
    async def health(request: Request):
        runtime: ApplicationRuntime = request.app.state.runtime
        await asyncio.to_thread(_check_store, runtime)
        ready = runtime.ready()
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.get("/v1/capabilities", response_model=Capabilities, responses=ERROR_RESPONSES)
    async def capabilities(request: Request):
        runtime = request.app.state.runtime
        real = runtime.social.backend == 'sau' and runtime.social.sending_enabled
        return Capabilities(mode="fixture", video_generation=runtime.generator is not None, publishing=real, single_user=True, generation_verification="contract_tested_live_unverified" if runtime.generator else "unavailable", real_platform_sending=real)

    @app.get("/v1/runs", response_model=RunPage, responses=ERROR_RESPONSES)
    async def list_runs(request: Request, limit: str = "30", before: str | None = None):
        try:
            page_size = int(limit)
            cursor = int(before) if before is not None else None
            if not 1 <= page_size <= 100 or (cursor is not None and cursor < 1):
                raise ValueError
        except ValueError:
            raise Problem("INVALID_INPUT", "Use limit 1–100 and a positive before cursor.", 400) from None
        page = await asyncio.to_thread(request.app.state.runtime.store.list_runs, cursor, page_size)
        return RunPage.model_validate(page)

    @app.post("/v1/runs", response_model=RunSnapshot, responses={**ERROR_RESPONSES, 202: {"model": RunSnapshot}})
    async def create_run(request: Request, body: FixtureCreate | MPTCreate, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if isinstance(body, MPTCreate):
            model = body
            normalized = {"mode": model.mode, "title": model.title}
        else:
            model = body
            normalized = {"mode": "fixture", "title": model.title, "scenario": model.scenario, "delay_ms": model.delay_ms}
        runtime = request.app.state.runtime
        try:
            snapshot, created = await asyncio.to_thread(runtime.store.create, normalized, idempotency_key, request.state.trace)
        except Problem:
            raise
        if created:
            runtime.worker.wake.set()
        request.scope["path_params"]["run_id"] = snapshot["run_id"]
        return _snapshot_response(snapshot, 202 if created else 200)

    @app.get("/v1/runs/{run_id}", response_model=RunSnapshot, responses=ERROR_RESPONSES)
    async def get_run(run_id: str, request: Request):
        if not re.fullmatch(ID, run_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        return RunSnapshot.model_validate(await asyncio.to_thread(request.app.state.runtime.store.snapshot, run_id))

    async def apply_command(run_id, action, request, body, idempotency_key):
        if not re.fullmatch(ID, run_id) or action not in {"cancel", "retry"}:
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        model = body
        runtime = request.app.state.runtime
        snapshot, accepted = await asyncio.to_thread(runtime.store.command, run_id, action, idempotency_key, model.expected_version, request.state.trace)
        runtime.worker.wake.set()
        return _snapshot_response(snapshot, 202 if accepted else 200)

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunSnapshot, responses={**ERROR_RESPONSES, 202: {"model": RunSnapshot}})
    async def cancel_run(run_id: str, request: Request, body: ExpectedVersion, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        return await apply_command(run_id, "cancel", request, body, idempotency_key)

    @app.post("/v1/runs/{run_id}/retry", response_model=RunSnapshot, responses={**ERROR_RESPONSES, 202: {"model": RunSnapshot}})
    async def retry_run(run_id: str, request: Request, body: ExpectedVersion, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        return await apply_command(run_id, "retry", request, body, idempotency_key)

    @app.get("/v1/runs/{run_id}/events", responses={**ERROR_RESPONSES, 200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    async def run_events(run_id: str, request: Request, after: str | None = None, last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
        runtime = request.app.state.runtime
        raw = last_event_id if last_event_id is not None else (after if after is not None else "")
        try:
            cursor = int(raw)
        except (ValueError, TypeError):
            raise Problem("RESYNC_REQUIRED", "Fetch a snapshot and supply its event cursor.", 409, run_id) from None
        await asyncio.to_thread(runtime.store.events, run_id, cursor)
        if not runtime.stream_slots.acquire(blocking=False):
            raise Problem("STREAM_LIMIT", "Too many open event streams. Close an older view.", 429, run_id)
        with runtime.stream_lock:
            runtime.stream_count += 1

        async def frames():
            nonlocal cursor
            try:
                yield ": connected\n\n"
                heartbeat = asyncio.get_running_loop().time()
                while not runtime.stopping.is_set():
                    events = await asyncio.to_thread(runtime.store.events, run_id, cursor)
                    for event in events:
                        value = _event_snapshot(event["payload"])
                        yield f"id: {event['id']}\nevent: snapshot\ndata: {value.model_dump_json()}\n\n"
                        cursor = event["id"]
                    current = await asyncio.to_thread(runtime.store.snapshot, run_id)
                    if cursor == current["last_event_id"] and current["state"] in TERMINAL and not any(p["state"] in ("PREPARED", "SENDING") for p in current["publications"]):
                        current = RunSnapshot.model_validate(current)
                        yield "event: complete\ndata: " + current.model_dump_json() + "\n\n"
                        break
                    now = asyncio.get_running_loop().time()
                    if now - heartbeat >= 5:
                        yield ": heartbeat\nevent: heartbeat\ndata: {}\n\n"
                        heartbeat = now
                    await asyncio.sleep(.05)
            except (asyncio.CancelledError, OSError, RuntimeError):
                return
            finally:
                with runtime.stream_lock:
                    runtime.stream_count -= 1
                runtime.stream_slots.release()

        return StreamingResponse(frames(), media_type="text/event-stream; charset=utf-8", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/v1/runs/{run_id}/publications", response_model=RunSnapshot, responses=ERROR_RESPONSES)
    async def create_publication(run_id: str, request: Request, body: PublicationCreate, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if not re.fullmatch(ID, run_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        model = body
        values = {"platform": model.platform, "account": model.account}
        result = await asyncio.to_thread(request.app.state.runtime.store.publication_create, run_id, values, idempotency_key, model.expected_version, request.state.trace)
        return _snapshot_response(result)

    async def dispatch_publication(run_id, intent_id, request, body, idempotency_key):
        if not re.fullmatch(ID, run_id) or not re.fullmatch(ID, intent_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        runtime = request.app.state.runtime
        model = body
        snapshot, job = await asyncio.to_thread(runtime.store.publication_dispatch, run_id, intent_id, idempotency_key, model.expected_version, model.scenario, request.state.trace)
        if job:
            runtime.publisher.submit(job, model.scenario)
        return _snapshot_response(snapshot, 202 if job else 200)

    @app.post("/v1/runs/{run_id}/publications/{intent_id}/dispatch", response_model=RunSnapshot, responses={**ERROR_RESPONSES, 202: {"model": RunSnapshot}})
    async def publication_dispatch(run_id: str, intent_id: str, request: Request, body: PublicationDispatch, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if not re.fullmatch(ID, run_id) or not re.fullmatch(ID, intent_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        return await dispatch_publication(run_id, intent_id, request, body, idempotency_key)

    @app.post("/v1/runs/{run_id}/publications/{intent_id}/verify", response_model=RunSnapshot, responses=ERROR_RESPONSES)
    async def publication_verify(run_id: str, intent_id: str, request: Request, body: PublicationVerify, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if not re.fullmatch(ID, run_id) or not re.fullmatch(ID, intent_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        result = await asyncio.to_thread(request.app.state.runtime.store.publication_verify, run_id, intent_id, idempotency_key, body.expected_version, request.state.trace)
        return _snapshot_response(result)

    @app.get("/v1/artifacts/{artifact_id}/content", responses={**ERROR_RESPONSES, 200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
    async def artifact(artifact_id: str, request: Request):
        if not re.fullmatch(ID, artifact_id):
            raise Problem("NOT_FOUND", "This address does not exist.", 404)
        info, content = await asyncio.to_thread(request.app.state.runtime.store.artifact, artifact_id)
        return Response(content, media_type=info["media_type"], headers={"Content-Length": str(len(content)), "Content-Disposition": "inline"})

    register_studio_routes(app, ERROR_RESPONSES)

    @app.api_route("/v1", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    @app.api_route("/v1/{unknown:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    @app.api_route("/{unknown:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    async def not_found(unknown: str = ""):
        raise Problem("NOT_FOUND", "This address does not exist.", 404)

    return app


def _check_store(runtime):
    with runtime.store.transaction() as db:
        db.execute("SELECT 1 FROM schema_version").fetchone()
