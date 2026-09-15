from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api.handoffs import router as handoffs_router
from app.api.routes import router
from app.api.runtime_profiles import router as profiles_router
from app.config import get_settings
from app.infrastructure.database import Conversation, SessionLocal, init_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_schema:
        await init_database()
    from app.application.runtime_profiles import initialize_profiles

    async with SessionLocal() as session:
        try:
            await initialize_profiles(session)
        except OperationalError:
            # Readiness reports pending migrations; startup must remain observable.
            await session.rollback()
    yield


app = FastAPI(
    title="SkillX Workflow API",
    version="0.1.0",
    description="契约驱动的持久化工作流平台 API",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")
app.include_router(profiles_router, prefix="/api")
app.include_router(handoffs_router, prefix="/api")


@app.middleware("http")
async def selected_runtime(request: Request, call_next):
    from app.application.runtime_config import agent_runtime_scope
    from app.application.runtime_profiles import agent_snapshot, restore_agent_snapshot

    # Request-local selection: semantic planning and queued execution use the same Agent.
    if (
        not request.url.path.startswith("/api/")
        or request.url.path.startswith("/api/runtime-profiles")
        or request.url.path.startswith("/api/health/live")
    ):
        return await call_next(request)
    profile_id = None
    parts = request.url.path.split("/")
    async with SessionLocal() as session:
        if len(parts) > 3 and parts[2] == "conversations":
            conversation = await session.get(Conversation, parts[3])
            if conversation is not None:
                profile_id = conversation.agent_profile_id
        try:
            runtime = restore_agent_snapshot(await agent_snapshot(session, profile_id))
        except OperationalError:
            if request.url.path == "/api/health/ready":
                return await call_next(request)
            raise
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
    with agent_runtime_scope(runtime):
        return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = request.headers.get("x-request-id", str(uuid4()))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "请求参数校验失败",
                "details": exc.errors(),
                "requestId": request_id,
            }
        },
    )
