from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.infrastructure.database import init_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_schema:
        await init_database()
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
