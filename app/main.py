import logging
import traceback
from urllib.parse import urlsplit
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from . import auth, chat, library_api, operations
from .core import *

app = FastAPI(
    title="党建 AI 智能问答系统",
    version="1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
for module in (auth, chat, library_api, operations):
    app.include_router(module.routes, prefix="/api/v1")
templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "app/static")), name="static")


@app.middleware("http")
async def envelope(request: Request, call_next):
    request.state.request_id = str(uuid4())
    if request.url.path.endswith("/questions"):
        try:
            request.state.request_id = str(
                UUID(request.headers.get("idempotency-key", ""))
            )
        except ValueError:
            pass
    local = request.client is None or request.client.host in (
        "127.0.0.1",
        "::1",
        "testclient",
    )
    if not local and request.url.scheme != "https":
        return result(
            request, code="HTTPS_REQUIRED", message="远程访问请使用 HTTPS。", status=403
        )
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and (
            urlsplit(origin).netloc != request.url.netloc
            or urlsplit(origin).scheme != request.url.scheme
        ):
            return result(
                request, code="ACCESS_DENIED", message="请求来源不匹配。", status=403
            )
    try:
        reply = await call_next(request)
    except Exception as exc:
        # Record the exception type and code locations, never request data or SQL parameters.
        from pathlib import Path

        frames = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}"
            for f in traceback.extract_tb(exc.__traceback__)[-5:]
        )
        logging.error("%s %s %s", request.state.request_id, type(exc).__name__, frames)
        reply = result(
            request,
            code="SERVICE_ERROR",
            message="处理失败，请记录请求标识并重试。",
            status=503,
        )
    reply.headers["X-Request-Id"] = request.state.request_id
    reply.headers["X-Content-Type-Options"] = "nosniff"
    reply.headers["Referrer-Policy"] = "same-origin"
    reply.headers["X-Frame-Options"] = "SAMEORIGIN"
    reply.headers["Cache-Control"] = (
        "no-store"
        if not request.url.path.startswith("/static/")
        else "public,max-age=300"
    )
    return reply


@app.exception_handler(Problem)
async def business_error(req, exc):
    return result(req, code=exc.code, message=exc.message, status=exc.status)


@app.exception_handler(RequestValidationError)
async def invalid(req, exc):
    return result(
        req, code="INVALID_ARGUMENT", message="请求字段或格式无效。", status=400
    )


@app.exception_handler(IntegrityError)
async def constraint(req, exc):
    return result(
        req,
        code="VERSION_CONFLICT",
        message="名称重复或记录仍被引用，请刷新后核对。",
        status=409,
    )


@app.get("/health")
def health():
    with engine().connect() as db:
        run(db, "SELECT 1 FROM alembic_version")
    return {"status": "ok", "project": "dangjian-knowledge-qa"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})
