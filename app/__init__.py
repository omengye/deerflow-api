"""DeerFlow API Service — FastAPI wrapper around DeerFlow harness."""
import logging
import sys
import asyncio
from pathlib import Path

logger = logging.getLogger(__name__)

# Load .env from project root before any config/settings are imported.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

# Prefer the embedded deerflow package in this API project. Fall back to the
# original harness checkout only when the embedded package is absent.
_project_root = Path(__file__).resolve().parent.parent
_embedded_harness_package = _project_root / "deerflow"
_harness_path = _project_root.parent / "deer-flow" / "backend" / "packages" / "harness"
if not _embedded_harness_package.exists() and str(_harness_path) not in sys.path:
    sys.path.insert(0, str(_harness_path))

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from app.config import settings
from app.dependencies import get_client_manager
from app.middleware import ApiKeyAuthMiddleware, RequestContextMiddleware


_ADMIN_UI_DIR = _project_root / "admin-ui"


def _admin_ui_file_response(asset_path: str = "index.html") -> FileResponse:
    """Serve admin UI assets only when API authentication is enabled."""
    if not settings.auth_enabled:
        raise HTTPException(status_code=404, detail="Admin UI is disabled")
    if not _ADMIN_UI_DIR.exists():
        raise HTTPException(status_code=404, detail="Admin UI not found")

    root = _ADMIN_UI_DIR.resolve()
    target = (root / asset_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Admin UI asset not found") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Admin UI asset not found")
    return FileResponse(target)


def _install_proactor_shutdown_noise_filter() -> None:
    """Silence a known-benign Windows ProactorEventLoop teardown error.

    On Windows, ProactorEventLoop's pipe/socket transports can schedule a
    deferred ``call_soon(self._call_connection_lost, ...)`` during
    ``transport.close()``; if the loop is already closed by the time that
    callback runs (observed with anyio/httpx async stream cleanup on
    Python 3.14), asyncio's default exception handler logs it as an
    unhandled ``RuntimeError('Event loop is closed')``. This is a
    long-standing, still-open upstream issue (see cpython gh-149388,
    encode/httpx discussions#2959) rather than something this app can fix;
    it does not fail in-flight requests -- it only produces noisy logs
    during transport GC/teardown.

    A previous fix forced WindowsSelectorEventLoopPolicy globally via the
    now-deprecated asyncio.set_event_loop_policy(). That is stronger than
    needed and has a real cost: SelectorEventLoop cannot create subprocess
    transports on Windows at all (NotImplementedError), which silently
    breaks this project's default "stdio" MCP transport (deerflow.mcp.client
    spawns MCP servers via subprocess). Since the underlying RuntimeError is
    cosmetic (log noise, not a request failure -- confirmed by running the
    full test suite and a targeted streaming-abandonment repro against the
    default ProactorEventLoop without any policy override), we keep the
    default Proactor loop (preserving subprocess/MCP-stdio support) and
    instead filter just this one known-benign error out of the loop's
    exception handler.
    """
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, RuntimeError) and str(exc) == "Event loop is closed":
            logger.debug("Suppressed benign Proactor shutdown error: %s", context.get("message"))
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    if sys.platform == "win32":
        _install_proactor_shutdown_noise_filter()
    manager = get_client_manager()
    await manager.startup()
    evolution_worker = None
    try:
        from deerflow.config import get_app_config

        if get_app_config().skill_evolution.enabled:
            from deerflow.skills.evolution.worker import get_evolution_worker

            evolution_worker = get_evolution_worker()
            evolution_worker.start(recover=True)
        yield
    finally:
        if evolution_worker is not None:
            evolution_worker.stop()
        await manager.shutdown()


app = FastAPI(
    title="DeerFlow API",
    description="API service built on DeerFlow agent harness",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiKeyAuthMiddleware)
app.add_middleware(RequestContextMiddleware)

# Import routers
from app.routers import admin, chat, threads, models, skills, mcp, uploads, runs, openai_compatible  # noqa: E402

app.include_router(chat.router, prefix="/api")
app.include_router(threads.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(openai_compatible.router)


@app.get("/management/", include_in_schema=False)
def management_index():
    return _admin_ui_file_response()


@app.get("/management", include_in_schema=False)
def management_redirect():
    _admin_ui_file_response()
    return RedirectResponse(url="/management/", status_code=307)


@app.get("/management/{asset_path:path}", include_in_schema=False)
def management_asset(asset_path: str):
    return _admin_ui_file_response(asset_path)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    manager = get_client_manager()
    payload = manager.readiness_check()
    if payload.get("status") != "ok":
        raise HTTPException(status_code=503, detail=payload)
    return payload
